"""Envoy's configuration for the cell egress proxy, rendered from Python (SSC-053).

Envoy runs as an explicit ``CONNECT`` proxy on ``PROXY_PORT``. The bootstrap is fixed: one
``dynamic_forward_proxy`` cluster, which resolves each allowed host through public resolvers
over TCP (the cell's DNS answers only Google's names), IPv4 only, and the listener read from a
file (LDS) that ``ssc_egress.runner`` rewrites whenever the org's snapshot changes it.

The listener, in order:

1. ``basic_auth`` on ``Proxy-Authorization``: the user is ``<env_id>.<credential_id>`` and only
   the credentials of active environments are listed, so a stopped app's tunnels are refused.
   A failure is ``407`` with ``Proxy-Authenticate``. With no credential at all every request is
   ``407``.
2. Routes: one per allowlist entry, matching a ``CONNECT`` whose authority is that host on port
   ``TUNNEL_PORT`` (``ssc_contracts.egress.authority_regex``: a ``*.`` stands for one label);
   it opens a TCP tunnel to the host. Any other ``CONNECT``, a raw IP address included, is
   ``403`` with a message naming the authority and how to request it. Anything that is not a
   ``CONNECT`` is ``405``: the proxy never forwards plain HTTP.

The listener is HTTP/1.1 over TCP only, so there is no ``CONNECT-UDP`` and no QUIC. A changed
listener replaces the old one, which Envoy drains: started with ``--drain-time-s`` set to
``DRAIN_SECONDS`` and ``--drain-strategy immediate``, every tunnel the old listener holds is
closed within that time, so a removed host or a revoked credential stops working at once.

``python -m ssc_egress.envoy`` prints the bootstrap and an example listener; CI checks both with
``envoy --mode validate``.
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from ssc_contracts.egress import (
    DRAIN_SECONDS,
    PROXY_PORT,
    authority_regex,
    credential_user,
    refusal_message,
)
from ssc_shared.access import AccessView

ENVOY_VERSION: Final = "1.39.0"
PUBLIC_RESOLVERS: Final = ("8.8.8.8", "8.8.4.4")
USER_HEADER: Final = "x-ssc-proxy-user"
REALM: Final = 'Basic realm="ssc-egress"'
LISTENER: Final = "egress"
CLUSTER: Final = "egress"
LDS_FILE: Final = "lds.json"
RE2_PROGRAM_SIZE: Final = 4000
"""Envoy refuses a regex whose RE2 program is larger; a 253-character pattern stays below."""
NOT_AUTHENTICATED: Final = (
    "The cell's egress proxy needs this app environment's own credential, which HTTPS_PROXY "
    "carries; it is missing, revoked, or the app is stopped."
)
NOT_CONNECT: Final = "The cell's egress proxy opens HTTPS tunnels (CONNECT) only."
_T: Final = "type.googleapis.com/envoy.extensions."
_CONNECT: Final = {"upgrade_type": "CONNECT"}


@dataclass(frozen=True, slots=True, kw_only=True)
class EgressConfig:
    """``resolvers`` empty uses the machine's own resolver (tests only); ``lds_dir`` is where
    the runner writes ``LDS_FILE``."""

    port: int = PROXY_PORT
    lds_dir: str = "/tmp/ssc-egress"  # noqa: S108  (the container's own memory)
    resolvers: tuple[str, ...] = PUBLIC_RESOLVERS
    max_connections: int = 2000


@dataclass(frozen=True, slots=True)
class Policy:
    """What the listener allows: allowlist entries and ``(user, base64 SHA-1)`` credentials."""

    hosts: tuple[str, ...] = ()
    users: tuple[tuple[str, str], ...] = field(default=())


def policy_of(view: AccessView | None) -> Policy:
    """The view's allowlist and the credentials of its active environments, sorted, so the same
    snapshot content always renders the same listener."""
    if view is None or view.egress is None:
        return Policy()
    users = [
        (credential_user(env_id, c.credential_id), c.sha1)
        for env_id, held in view.egress.credentials.items()
        if (env := view.environments.get(env_id)) is not None and env.active
        for c in held
    ]
    return Policy(tuple(sorted(view.egress.hosts)), tuple(sorted(users)))


def _dns_cache(cfg: EgressConfig) -> dict[str, Any]:
    cache: dict[str, Any] = {"name": "egress", "dns_lookup_family": "V4_ONLY"}
    if cfg.resolvers:
        cache["typed_dns_resolver_config"] = {
            "name": "envoy.network.dns_resolver.cares",
            "typed_config": {
                "@type": _T + "network.dns_resolver.cares.v3.CaresDnsResolverConfig",
                "resolvers": [
                    {"socket_address": {"address": a, "port_value": 53}} for a in cfg.resolvers
                ],
                "use_resolvers_as_fallback": False,
                "dns_resolver_options": {
                    "use_tcp_for_dns_lookups": True,
                    "no_default_search_domain": True,
                },
            },
        }
    return cache


def bootstrap(cfg: EgressConfig) -> dict[str, Any]:
    """The fixed part: the forward-proxy cluster and the listener read from ``LDS_FILE``."""
    return {
        "node": {"id": "ssc-egress", "cluster": "ssc-egress"},
        "dynamic_resources": {
            "lds_config": {
                "resource_api_version": "V3",
                "path_config_source": {
                    "path": f"{cfg.lds_dir}/{LDS_FILE}",
                    "watched_directory": {"path": cfg.lds_dir},
                },
            }
        },
        "static_resources": {
            "clusters": [
                {
                    "name": CLUSTER,
                    "connect_timeout": "5s",
                    "lb_policy": "CLUSTER_PROVIDED",
                    "cluster_type": {
                        "name": "envoy.clusters.dynamic_forward_proxy",
                        "typed_config": {
                            "@type": _T + "clusters.dynamic_forward_proxy.v3.ClusterConfig",
                            "dns_cache_config": _dns_cache(cfg),
                        },
                    },
                }
            ]
        },
        "layered_runtime": {
            "layers": [
                {
                    "name": "static",
                    "static_layer": {"re2.max_program_size.error_level": RE2_PROGRAM_SIZE},
                }
            ]
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


def _reply(status: int, body: str, code: int | None = None) -> dict[str, Any]:
    mapper: dict[str, Any] = {
        "filter": {
            "status_code_filter": {
                "comparison": {
                    "op": "EQ",
                    "value": {"default_value": status, "runtime_key": f"ssc_egress_{status}"},
                }
            }
        },
        "body_format_override": {
            "text_format_source": {"inline_string": body + "\n"},
            "content_type": "text/plain; charset=utf-8",
        },
    }
    if code is not None:
        mapper["status_code"] = code
        mapper["headers_to_add"] = [
            {
                "header": {"key": "proxy-authenticate", "value": REALM},
                "append_action": "OVERWRITE_IF_EXISTS_OR_ADD",
            }
        ]
    return mapper


def _allow(index: int, pattern: str) -> dict[str, Any]:
    return {
        "name": f"allow-{index}",
        "match": {
            "connect_matcher": {},
            "headers": [
                {
                    "name": ":authority",
                    "string_match": {"safe_regex": {"regex": authority_regex(pattern)}},
                }
            ],
        },
        "route": {
            "cluster": CLUSTER,
            "timeout": "0s",
            "upgrade_configs": [{**_CONNECT, "connect_config": {}}],
        },
    }


def _routes(policy: Policy) -> list[dict[str, Any]]:
    if not policy.users:
        return [
            {"name": name, "match": match, "direct_response": {"status": 401}}
            for name, match in (
                ("no-credentials", {"connect_matcher": {}}),
                ("no-credentials-plain", {"prefix": ""}),
            )
        ]
    return [
        *(_allow(i, p) for i, p in enumerate(policy.hosts)),
        {"name": "refuse", "match": {"connect_matcher": {}}, "direct_response": {"status": 403}},
        {"name": "not-connect", "match": {"prefix": ""}, "direct_response": {"status": 405}},
    ]


def _htpasswd(users: Iterable[tuple[str, str]]) -> str:
    return "".join(f"{user}:{{SHA}}{digest}\n" for user, digest in users)


def listener(cfg: EgressConfig, policy: Policy) -> dict[str, Any]:
    """The listener for ``policy``."""
    filters: list[dict[str, Any]] = []
    if policy.users:
        filters.append(
            {
                "name": "envoy.filters.http.basic_auth",
                "typed_config": {
                    "@type": _T + "filters.http.basic_auth.v3.BasicAuth",
                    "users": {"inline_string": _htpasswd(policy.users)},
                    "forward_username_header": USER_HEADER,
                    "authentication_header": "proxy-authorization",
                },
            }
        )
    filters += [
        {
            "name": "envoy.filters.http.dynamic_forward_proxy",
            "typed_config": {
                "@type": _T + "filters.http.dynamic_forward_proxy.v3.FilterConfig",
                "dns_cache_config": _dns_cache(cfg),
            },
        },
        {
            "name": "envoy.filters.http.router",
            "typed_config": {
                "@type": _T + "filters.http.router.v3.Router",
                "suppress_envoy_headers": True,
            },
        },
    ]
    log_format = {
        "at": "%START_TIME%",
        "user": f"%REQ({USER_HEADER})%",
        "authority": "%REQ(:AUTHORITY)%",
        "status": "%RESPONSE_CODE%",
        "flags": "%RESPONSE_FLAGS%",
        "upstream": "%UPSTREAM_HOST%",
        "sent": "%BYTES_SENT%",
        "received": "%BYTES_RECEIVED%",
        "ms": "%DURATION%",
    }
    hcm = {
        "@type": _T + "filters.network.http_connection_manager.v3.HttpConnectionManager",
        "stat_prefix": "egress",
        "codec_type": "HTTP1",
        "request_headers_timeout": "10s",
        "upgrade_configs": [_CONNECT],
        "access_log": [
            {
                "name": "envoy.access_loggers.stdout",
                "typed_config": {
                    "@type": _T + "access_loggers.stream.v3.StdoutAccessLog",
                    "log_format": {"json_format": log_format},
                },
            }
        ],
        "route_config": {
            "name": "egress",
            "virtual_hosts": [{"name": "egress", "domains": ["*"], "routes": _routes(policy)}],
        },
        "local_reply_config": {
            "mappers": [
                _reply(401, NOT_AUTHENTICATED, 407),
                _reply(403, refusal_message("%REQ(:AUTHORITY)%")),
                _reply(405, NOT_CONNECT),
            ]
        },
        "http_filters": filters,
    }
    return {
        "@type": "type.googleapis.com/envoy.config.listener.v3.Listener",
        "name": LISTENER,
        "address": {"socket_address": {"address": "0.0.0.0", "port_value": cfg.port}},  # noqa: S104  (the cell's apps reach it)
        "filter_chains": [
            {
                "filters": [
                    {"name": "envoy.filters.network.http_connection_manager", "typed_config": hcm}
                ]
            }
        ],
    }


def lds_document(cfg: EgressConfig, policy: Policy) -> dict[str, Any]:
    """The file Envoy reads: the listener, versioned by its own digest."""
    resource = listener(cfg, policy)
    version = hashlib.sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()[:16]
    return {"version_info": version, "resources": [resource]}


def envoy_args(config_path: str) -> list[str]:
    """Envoy's command line after the binary."""
    return [
        "-c",
        config_path,
        "--log-level",
        "warn",
        "--disable-hot-restart",
        "--drain-time-s",
        str(DRAIN_SECONDS),
        "--drain-strategy",
        "immediate",
    ]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ssc_egress.envoy")
    parser.add_argument("part", choices=("bootstrap", "listener"))
    parser.add_argument("--host", action="append", default=[], help="an allowlist entry")
    args = parser.parse_args(argv)
    cfg = EgressConfig()
    example = Policy(tuple(args.host), (("env_" + "a" * 20 + ".example00000", "A" * 27 + "="),))
    out = bootstrap(cfg) if args.part == "bootstrap" else lds_document(cfg, example)
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
