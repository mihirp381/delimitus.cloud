import json
from pathlib import Path

CANDIDATES = ["gcp", "aws", "azure", "fly"]


def g(d, *ks):
    for k in ks:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def sub(c, key):
    v = g(c, "egress", "detail", key, "blocked")
    return None if v is None else ("blocked" if v else "open")


def ms(c, app, k):
    v = g(c, "cold_start", app, k)
    return None if v is None else f"{v / 1000:.2f} s"


AUTO = [
    ("Direct egress blocked: TCP 443", lambda c: sub(c, "tcp_1.1.1.1_443")),
    ("Direct egress blocked: TCP 80", lambda c: sub(c, "tcp_1.1.1.1_80")),
    ("Direct egress blocked: UDP 53", lambda c: sub(c, "udp_dns_8.8.8.8_53")),
    ("Direct egress blocked: UDP 443 (QUIC)", lambda c: sub(c, "udp_quic_1.1.1.1_443")),
    ("Direct egress blocked: IPv6 TCP 443", lambda c: sub(c, "tcp6_2606:4700:4700::1111_443")),
    ("DNS exfil blocked (system resolver + direct UDP)", lambda c: g(c, "dns", "result")),
    ("Machine token has no permissions", lambda c: g(c, "metadata", "result")),
    ("Default address returns 403 from internet", lambda c: g(c, "public_ingress", "status")),
    ("Traffic only via internal LB, host intact", lambda c: g(c, "headers", "host_matches_request")),
    ("App cannot reach peer app", lambda c: g(c, "peer", "result")),
    ("Cold start static p50 (<1.5 s)", lambda c: ms(c, "static", "p50_ms")),
    ("Cold start static max", lambda c: ms(c, "static", "max_ms")),
    ("Cold start Python API p50 (<3 s)", lambda c: ms(c, "api", "p50_ms")),
    ("Cold start Python API max", lambda c: ms(c, "api", "max_ms")),
    ("Cold start Streamlit p50 (<10 s)", lambda c: ms(c, "streamlit", "p50_ms")),
    ("Cold start Streamlit max", lambda c: ms(c, "streamlit", "max_ms")),
    ("WebSocket held (target 1800 s)", lambda c: None if g(c, "ws", "held_s") is None else f"{g(c, 'ws', 'held_s')} s"),
    ("SSE held (target 600 s)", lambda c: None if g(c, "sse", "held_s") is None else f"{g(c, 'sse', 'held_s')} s"),
    ("Cut-off time (<10 s)", lambda c: None if g(c, "kill", "cut_off_s") is None else f"{g(c, 'kill', 'cut_off_s')} s"),
    ("Refuses all public ingress", lambda c: g(c, "public_ingress", "result")),
    ("Authorization header passes through unchanged", lambda c: g(c, "headers", "result")),
]

MANUAL = [
    ("Empty-cell monthly cost (USD)", "empty_cell_monthly_usd"),
    ("Streamlit WS origin check works behind proxy", "streamlit_ws_origin"),
    ("Org-level deny on reading secret values", "org_deny_secret_read"),
    ("One isolated account/project per customer", "isolated_account_per_customer"),
    ("MicroVM or equivalent isolation between apps", "microvm_isolation"),
    ("Managed Postgres: PITR + customer-managed keys", "postgres_pitr_cmek"),
    ("Managed build service", "managed_build"),
    ("Managed log store with query API", "log_query_api"),
    ("Fixed outbound IP", "fixed_egress_ip"),
    ("Team operating experience (years)", "operating_experience_years"),
    ("Where likely customers' data lives", "customer_data_location"),
]


def main():
    results, manual = {}, {}
    for cand in CANDIDATES:
        p = Path(f"results/{cand}.json")
        if p.exists():
            results[cand] = json.loads(p.read_text()).get("checks", {})
        m = Path(f"results/{cand}.manual.json")
        if m.exists():
            manual[cand] = json.loads(m.read_text())
    cols = [c for c in CANDIDATES if c in results or c in manual]
    lines = ["# SSC-001 scorecard", "", "Fly is a cost and speed control only, not a candidate.", "",
             "| Check | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]

    def cell(v):
        return "" if v is None else str(v)

    for label, fn in AUTO:
        lines.append(f"| {label} | " + " | ".join(cell(fn(results.get(c, {}))) for c in cols) + " |")
    for label, key in MANUAL:
        lines.append(f"| {label} | " + " | ".join(cell(manual.get(c, {}).get(key)) for c in cols) + " |")
    Path("SCORECARD.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
