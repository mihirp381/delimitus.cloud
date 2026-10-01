"""Envoy's configuration for the gateway, rendered from Python (SSC-018, decision 023).

Filters, in order:

1. ``local_ratelimit``: one token bucket per gateway instance; ``429`` when empty.
2. ``strip`` (Lua): drops every request header starting ``x-ssc-`` or ``x-envoy-``, and
   ``X-Serverless-Authorization``, so nothing a caller sends is mistaken for the platform's, then
   copies ``Content-Length`` for the check.
3. ``ext_authz`` (HTTP) to ``ssc_edge.server`` on loopback. Fails closed: an unreachable, slow
   or failing service is ``503``.
4. ``route`` (Lua): moves the request to the service host the check named, keeps the app host
   in ``X-Forwarded-Host`` and drops platform cookies from ``Cookie``. On the response, and only
   for responses from an app, drops ``Set-Cookie`` values that use a platform name.
5. ``dynamic_forward_proxy`` and ``router``: forwards to that host over TLS, the name checked.

``python -m ssc_edge.envoy`` prints the JSON; CI checks it with ``envoy --mode validate``.
"""

import argparse
import ipaddress
import json
import sys
from dataclasses import dataclass
from typing import Any, Final

from ssc_edge import pages
from ssc_edge.gate import IDENTITY_HEADER, UPSTREAM_HEADER
from ssc_edge.server import AUTHZ_PREFIX, LENGTH_HEADER, SERVERLESS_AUTH
from ssc_edge.session import PLATFORM_PREFIXES

ENVOY_VERSION: Final = "1.39.0"
ALLOWED_HEADERS: Final = (
    "cookie",
    "origin",
    "upgrade",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    LENGTH_HEADER,
)
"""Request headers the check sees, besides ``Host``, method and path."""
UPSTREAM_HEADERS: Final = (IDENTITY_HEADER, UPSTREAM_HEADER, SERVERLESS_AUTH)
CA_BUNDLE: Final = "/etc/ssl/certs/ca-certificates.crt"

_T = "type.googleapis.com/envoy.extensions."

STRIP_LUA: Final = f"""
function envoy_on_request(h)
  local hs = h:headers()
  local drop = {{}}
  for k, _ in pairs(hs) do
    if string.sub(k, 1, 6) == "x-ssc-" or string.sub(k, 1, 8) == "x-envoy-"
        or k == "{SERVERLESS_AUTH}" then
      table.insert(drop, k)
    end
  end
  for _, k in ipairs(drop) do hs:remove(k) end
  local length = hs:get("content-length")
  if length ~= nil then hs:replace("{LENGTH_HEADER}", length) end
end
"""

_PLATFORM_LUA = " or ".join(f'string.sub(name, 1, {len(p)}) == "{p}"' for p in PLATFORM_PREFIXES)

ROUTE_LUA: Final = f"""
local function platform(name)
  name = string.lower(name)
  return {_PLATFORM_LUA}
end

function envoy_on_request(h)
  local hs = h:headers()
  local up = hs:get("{UPSTREAM_HEADER}")
  if up == nil or up == "" then
    h:respond({{[":status"] = "503"}}, "")
    return
  end
  hs:remove("{UPSTREAM_HEADER}")
  hs:remove("{LENGTH_HEADER}")
  hs:replace("x-forwarded-host", hs:get(":authority"))
  hs:replace(":authority", up)
  local keep = {{}}
  for i = 0, hs:getNumValues("cookie") - 1 do
    for part in string.gmatch(hs:getAtIndex("cookie", i), "[^;]+") do
      local item = string.match(part, "^%s*(.-)%s*$")
      local name = string.match(item, "^([^=]*)") or ""
      if item ~= "" and not platform(name) then table.insert(keep, item) end
    end
  end
  hs:remove("cookie")
  if #keep > 0 then hs:add("cookie", table.concat(keep, "; ")) end
  h:streamInfo():dynamicMetadata():set("ssc", "upstream", true)
end

function envoy_on_response(h)
  local meta = h:streamInfo():dynamicMetadata():get("ssc")
  if meta == nil or not meta["upstream"] then return end
  local hs = h:headers()
  local n = hs:getNumValues("set-cookie")
  if n == 0 then return end
  local keep = {{}}
  for i = 0, n - 1 do
    local v = hs:getAtIndex("set-cookie", i)
    local name = string.match(v, "^%s*([^=;%s]*)") or ""
    if not platform(name) then table.insert(keep, v) end
  end
  hs:remove("set-cookie")
  for _, v in ipairs(keep) do hs:add("set-cookie", v) end
end
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvoyConfig:
    port: int = 8080
    authz_host: str = "127.0.0.1"
    authz_port: int = 9001
    authz_timeout_ms: int = 500
    upstream_tls: bool = True
    rate_per_second: int = 500
    rate_burst: int = 1000
    max_connections: int = 10000


def _exact(names: tuple[str, ...]) -> list[dict[str, str]]:
    return [{"exact": n} for n in names]


def _lua(name: str, source: str) -> dict[str, Any]:
    return {
        "name": name,
        "typed_config": {
            "@type": _T + "filters.http.lua.v3.Lua",
            "default_source_code": {"inline_string": source},
        },
    }


def _dns_cache() -> dict[str, Any]:
    return {"name": "apps", "dns_lookup_family": "V4_ONLY"}


def _authz_cluster(cfg: EnvoyConfig) -> dict[str, Any]:
    try:
        ipaddress.ip_address(cfg.authz_host)
        kind = "STATIC"
    except ValueError:
        kind = "STRICT_DNS"
    address = {"socket_address": {"address": cfg.authz_host, "port_value": cfg.authz_port}}
    return {
        "name": "authz",
        "type": kind,
        "dns_lookup_family": "V4_ONLY",
        "connect_timeout": "0.25s",
        "load_assignment": {
            "cluster_name": "authz",
            "endpoints": [{"lb_endpoints": [{"endpoint": {"address": address}}]}],
        },
    }


def _apps_cluster(cfg: EnvoyConfig) -> dict[str, Any]:
    cluster: dict[str, Any] = {
        "name": "apps",
        "connect_timeout": "5s",
        "lb_policy": "CLUSTER_PROVIDED",
        "cluster_type": {
            "name": "envoy.clusters.dynamic_forward_proxy",
            "typed_config": {
                "@type": _T + "clusters.dynamic_forward_proxy.v3.ClusterConfig",
                "dns_cache_config": _dns_cache(),
            },
        },
    }
    if cfg.upstream_tls:
        cluster["typed_extension_protocol_options"] = {
            "envoy.extensions.upstreams.http.v3.HttpProtocolOptions": {
                "@type": _T + "upstreams.http.v3.HttpProtocolOptions",
                "upstream_http_protocol_options": {"auto_sni": True, "auto_san_validation": True},
                "explicit_http_config": {"http_protocol_options": {}},
            }
        }
        cluster["transport_socket"] = {
            "name": "envoy.transport_sockets.tls",
            "typed_config": {
                "@type": _T + "transport_sockets.tls.v3.UpstreamTlsContext",
                "common_tls_context": {
                    "validation_context": {"trusted_ca": {"filename": CA_BUNDLE}}
                },
            },
        }
    return cluster


def render(cfg: EnvoyConfig) -> dict[str, Any]:
    ext_authz = {
        "name": "envoy.filters.http.ext_authz",
        "typed_config": {
            "@type": _T + "filters.http.ext_authz.v3.ExtAuthz",
            "transport_api_version": "V3",
            "failure_mode_allow": False,
            "status_on_error": {"code": "ServiceUnavailable"},
            "allowed_headers": {"patterns": _exact(ALLOWED_HEADERS)},
            "http_service": {
                "server_uri": {
                    "uri": f"http://{cfg.authz_host}:{cfg.authz_port}",
                    "cluster": "authz",
                    "timeout": f"{cfg.authz_timeout_ms / 1000}s",
                },
                "path_prefix": AUTHZ_PREFIX,
                "authorization_response": {
                    "allowed_upstream_headers": {"patterns": _exact(UPSTREAM_HEADERS)},
                    "allowed_client_headers": {"patterns": _exact(pages.CLIENT_HEADERS)},
                },
            },
        },
    }
    ratelimit = {
        "name": "envoy.filters.http.local_ratelimit",
        "typed_config": {
            "@type": _T + "filters.http.local_ratelimit.v3.LocalRateLimit",
            "stat_prefix": "gateway",
            "token_bucket": {
                "max_tokens": cfg.rate_burst,
                "tokens_per_fill": cfg.rate_per_second,
                "fill_interval": "1s",
            },
            "filter_enabled": {
                "runtime_key": "ssc_rl_enabled",
                "default_value": {"numerator": 100, "denominator": "HUNDRED"},
            },
            "filter_enforced": {
                "runtime_key": "ssc_rl_enforced",
                "default_value": {"numerator": 100, "denominator": "HUNDRED"},
            },
        },
    }
    hcm = {
        "@type": _T + "filters.network.http_connection_manager.v3.HttpConnectionManager",
        "stat_prefix": "ingress",
        "normalize_path": True,
        "merge_slashes": True,
        "request_headers_timeout": "10s",
        "stream_idle_timeout": "3600s",
        "upgrade_configs": [{"upgrade_type": "websocket"}],
        "route_config": {
            "name": "apps",
            "virtual_hosts": [
                {
                    "name": "apps",
                    "domains": ["*"],
                    "routes": [
                        {"match": {"prefix": "/"}, "route": {"cluster": "apps", "timeout": "0s"}}
                    ],
                }
            ],
        },
        "http_filters": [
            ratelimit,
            _lua("ssc.strip", STRIP_LUA),
            ext_authz,
            _lua("ssc.route", ROUTE_LUA),
            {
                "name": "envoy.filters.http.dynamic_forward_proxy",
                "typed_config": {
                    "@type": _T + "filters.http.dynamic_forward_proxy.v3.FilterConfig",
                    "dns_cache_config": _dns_cache(),
                },
            },
            {
                "name": "envoy.filters.http.router",
                "typed_config": {
                    "@type": _T + "filters.http.router.v3.Router",
                    "suppress_envoy_headers": True,
                },
            },
        ],
    }
    listener = {
        "name": "ingress",
        "address": {"socket_address": {"address": "0.0.0.0", "port_value": cfg.port}},  # noqa: S104  (Cloud Run)
        "filter_chains": [
            {
                "filters": [
                    {"name": "envoy.filters.network.http_connection_manager", "typed_config": hcm}
                ]
            }
        ],
    }
    return {
        "static_resources": {
            "listeners": [listener],
            "clusters": [_authz_cluster(cfg), _apps_cluster(cfg)],
        },
        "overload_manager": {
            "resource_monitors": [
                {
                    "name": "envoy.resource_monitors.global_downstream_max_connections",
                    "typed_config": {
                        "@type": _T + "resource_monitors.downstream_connections.v3."
                        "DownstreamConnectionsConfig",
                        "max_active_downstream_connections": cfg.max_connections,
                    },
                }
            ]
        },
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ssc_edge.envoy")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--authz", default="127.0.0.1:9001", help="host:port of ssc_edge.server")
    parser.add_argument("--plaintext-upstream", action="store_true", help="tests only")
    args = parser.parse_args(argv)
    host, _, port = str(args.authz).rpartition(":")
    cfg = EnvoyConfig(
        port=args.port,
        authz_host=host,
        authz_port=int(port),
        upstream_tls=not args.plaintext_upstream,
    )
    json.dump(render(cfg), sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
