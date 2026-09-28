import json
import os
import secrets
import socket
import struct
import time
import urllib.error
import urllib.request

TIMEOUT = 3.0


def _timed(fn):
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def _tcp(host, port, family=socket.AF_INET):
    def run():
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        try:
            s.connect((host, port))
            return {"blocked": False, "detail": "connected"}
        except (OSError, socket.timeout) as e:
            return {"blocked": True, "detail": f"{type(e).__name__}: {e}"}
        finally:
            s.close()
    return _timed(run)


def _dns_query_bytes(name):
    tid = secrets.token_bytes(2)
    header = tid + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return header + q + struct.pack(">HH", 1, 1)


def _udp(host, port, payload, family=socket.AF_INET):
    def run():
        s = socket.socket(family, socket.SOCK_DGRAM)
        s.settimeout(TIMEOUT)
        try:
            s.sendto(payload, (host, port))
            data, _ = s.recvfrom(4096)
            return {"blocked": False, "detail": f"reply {len(data)} bytes"}
        except (OSError, socket.timeout) as e:
            return {"blocked": True, "detail": f"{type(e).__name__}: {e}"}
        finally:
            s.close()
    return _timed(run)


def egress():
    subs = {
        "tcp_1.1.1.1_443": _tcp("1.1.1.1", 443),
        "tcp_1.1.1.1_80": _tcp("1.1.1.1", 80),
        "udp_dns_8.8.8.8_53": _udp("8.8.8.8", 53, _dns_query_bytes("example.com")),
        "udp_quic_1.1.1.1_443": _udp("1.1.1.1", 443, secrets.token_bytes(1200)),
        "tcp6_2606:4700:4700::1111_443": _tcp("2606:4700:4700::1111", 443, socket.AF_INET6),
    }
    all_blocked = all(v.get("blocked") is True for v in subs.values())
    return {
        "check": "direct_egress_blocked",
        "result": "pass" if all_blocked else "fail",
        "detail": subs,
    }


def dns(zone):
    label = secrets.token_hex(6)
    name = f"{label}.{zone}" if zone else None
    if not name:
        return {"check": "dns_exfil_blocked", "result": "unknown", "detail": "no canary zone given"}

    def resolve():
        try:
            infos = socket.getaddrinfo(name, None)
            return {"resolved": True, "answers": sorted({i[4][0] for i in infos})}
        except socket.gaierror as e:
            return {"resolved": False, "detail": f"gaierror: {e}"}

    system = _timed(resolve)
    direct = _udp("8.8.8.8", 53, _dns_query_bytes(name))
    ok = system.get("resolved") is False and direct.get("blocked") is True
    return {
        "check": "dns_exfil_blocked",
        "result": "pass" if ok else "fail",
        "detail": {"name": name, "system_resolver": system, "direct_udp_8.8.8.8": direct,
                   "note": "also check the canary zone's query log for this name"},
    }


def _http(url, method="GET", headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def metadata():
    out = {}

    def gcp():
        status, body = _http(
            "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"})
        if status != 200:
            return {"token": False, "status": status}
        tok = json.loads(body).get("access_token", "")
        st, _ = _http("https://cloudresourcemanager.googleapis.com/v1/projects",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=5)
        return {"token": True, "token_len": len(tok), "projects_list_status": st}

    def aws():
        st, tok = _http("http://169.254.169.254/latest/api/token", method="PUT",
                        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        if st != 200:
            return {"token": False, "status": st}
        st2, body = _http("http://169.254.169.254/latest/meta-data/iam/security-credentials/",
                          headers={"X-aws-ec2-metadata-token": tok.decode()})
        return {"token": True, "role_list_status": st2, "role_list_len": len(body)}

    def azure():
        st, body = _http(
            "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/",
            headers={"Metadata": "true"})
        if st != 200:
            return {"token": False, "status": st}
        return {"token": True, "token_len": len(json.loads(body).get("access_token", ""))}

    for name, fn in (("gcp", gcp), ("aws", aws), ("azure", azure)):
        out[name] = _timed(fn)
    got = [n for n, v in out.items() if v.get("token")]
    gcp_ok = out["gcp"].get("projects_list_status") in (401, 403)
    if not got:
        result = "pass"
    elif got == ["gcp"] and gcp_ok:
        result = "pass"
    else:
        result = "fail"
    return {"check": "machine_token_no_permissions", "result": result, "detail": out}


def peer(url):
    if not url:
        return {"check": "peer_unreachable", "result": "unknown", "detail": "no url"}

    def run():
        st, body = _http(url)
        return {"status": st, "len": len(body)}

    r = _timed(run)
    ok = "error" in r or not (200 <= r.get("status", 0) < 300)
    return {"check": "peer_unreachable", "result": "pass" if ok else "fail", "detail": r}


def headers(h):
    fwd = {k: v for k, v in h.items() if k.lower().startswith("x-forwarded")}
    return {
        "check": "headers_passthrough",
        "result": "unknown",
        "detail": {
            "authorization_len": len(h.get("authorization", "")),
            "host": h.get("host"),
            "forwarded": fwd,
            "port": os.environ.get("PORT"),
        },
    }
