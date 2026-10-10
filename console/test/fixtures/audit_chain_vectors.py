"""Writes audit-chain.json, the vectors test/audit-chain.test.ts checks the browser's port against.

Run from the repo root:

    uv run python console/test/fixtures/audit_chain_vectors.py

Each vector is an export file (base64 of its bytes) and the report the CLI's own check gives for
it: the file is read the way ``ssc audit verify`` reads it and handed to
``ssc_cli.audit_chain.check_lines``. Rows are built the way the control plane builds them
(``ssc_control.audit.chain`` for the canonical bytes and the hash, ``ssc_control.audit.export``
for the line), so a change to either shows up as a failing console test once this is run again.
"""

import base64
import dataclasses
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ssc_cli.audit_chain import check_lines
from ssc_control.audit.chain import GENESIS_HASH, canonical_bytes

ORG = "org_0123456789abcdefghij"
OUT = Path(__file__).with_name("audit-chain.json")
START = datetime(2026, 10, 1, 9, 30, 0, 123456, tzinfo=UTC)

EVENTS: list[dict[str, Any]] = [
    {"action": "org.created", "target": ("org", ORG), "after": {"name": "Acme"}},
    {
        "action": "app.created",
        "target": ("app", "app_0123456789abcdefghij"),
        "after": {"slug": "expenses", "name": "Dépenses 経費 \U0001f4b8", "note": 'a "quoted" \\ line\n\ttab \x01'},
    },
    {
        "action": "grant.added",
        "target": ("grant", "grant_1"),
        "before": None,
        "after": {
            "cpu": 1.0,
            "memory_gb": 0.5,
            "tiny": 1e-05,
            "small": 0.0001,
            "huge": 1e16,
            "wide": 1e15,
            "long": 123456789012345678.0,
            "sum": 0.1 + 0.2,
            "least": 5e-324,
            "most": 1.7976931348623157e308,
            "minus": -0.0,
            "e21": 1e21,
            "micro": 1.5e-07,
            "big": 2**70,
            "past_double": 9007199254740993,
            "neg": -3,
        },
        "via_agent": True,
        "client_id": "client_abc",
        "ip": "203.0.113.7",
    },
    {
        "action": "app.disabled",
        "target": ("app", "app_0123456789abcdefghij"),
        "before": {"status": "active", "\U0001f600": 1, "\uffee": 2, "z": [1, {"b": None, "a": False}]},
        "after": {"status": "disabled"},
        "policy": "pd_42",
        # A whole second, and an offset that is not UTC: the row shows the same instant in UTC.
        "at": datetime(2026, 10, 1, 15, 0, 3, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    },
    {"action": "audit.exported", "target": ("org", ORG), "after": {"format": "jsonl", "events": 4}},
]


def chain(events: list[dict[str, Any]], *, org: str = ORG) -> list[dict[str, Any]]:
    """The stored rows: what ``append_event`` writes for each event, in order."""
    rows: list[dict[str, Any]] = []
    prev = GENESIS_HASH
    for index, event in enumerate(events):
        seq = index + 1
        at = event.get("at") or START + timedelta(seconds=7 * index)
        kind, target_id = event["target"]
        canonical = canonical_bytes(
            {
                "org_id": org,
                "seq": seq,
                "at": at.isoformat(),
                "action": event["action"],
                "actor": {
                    "kind": "user",
                    "id": "user_0123456789abcdefghij",
                    "via_agent": event.get("via_agent", False),
                    "client_id": event.get("client_id"),
                    "ip": event.get("ip"),
                },
                "target": {"kind": kind, "id": target_id},
                "before": event.get("before"),
                "after": event.get("after"),
                "policy_decision_id": event.get("policy"),
            }
        )
        digest = hashlib.sha256(prev + canonical).digest()
        rows.append(
            {
                "seq": seq,
                "at": at,
                "action": event["action"],
                "actor_via_agent": event.get("via_agent", False),
                "actor_client_id": event.get("client_id"),
                "target_kind": kind,
                "target_id": target_id,
                "before": event.get("before"),
                "after": event.get("after"),
                "policy_decision_id": event.get("policy"),
                "canonical": canonical,
                "prev_hash": prev,
                "hash": digest,
            }
        )
        prev = digest
    return rows


def record(row: dict[str, Any]) -> dict[str, Any]:
    """One export row: ``event_record`` plus ``canonical``, as ``jsonl_line`` writes it."""
    return {
        "seq": row["seq"],
        "at": row["at"].astimezone(UTC).isoformat(),
        "action": row["action"],
        "actor": {
            "kind": "user",
            "id": "user_0123456789abcdefghij",
            "via_agent": row["actor_via_agent"],
            "client_id": row["actor_client_id"],
        },
        "target": {"kind": row["target_kind"], "id": row["target_id"]},
        "before": row["before"],
        "after": row["after"],
        "policy_decision_id": row["policy_decision_id"],
        "prev_hash": row["prev_hash"].hex(),
        "hash": row["hash"].hex(),
        "canonical": base64.b64encode(row["canonical"]).decode(),
    }


def line(rec: dict[str, Any]) -> bytes:
    return canonical_bytes(rec) + b"\n"


def export(records: list[dict[str, Any]]) -> bytes:
    return b"".join(line(r) for r in records)


def rehashed(rec: dict[str, Any], canonical: bytes) -> dict[str, Any]:
    """``rec`` carrying other canonical bytes, with the hash that goes with them."""
    prev = bytes.fromhex(rec["prev_hash"])
    return {
        **rec,
        "canonical": base64.b64encode(canonical).decode(),
        "hash": hashlib.sha256(prev + canonical).hexdigest(),
    }


def doc(rec: dict[str, Any]) -> dict[str, Any]:
    return json.loads(base64.b64decode(rec["canonical"]))


def vectors() -> dict[str, bytes]:
    good = [record(r) for r in chain(EVENTS)]
    short = [record(r) for r in chain([EVENTS[0], EVENTS[4]])]
    out: dict[str, bytes] = {}

    def last(change: Any) -> bytes:
        """A two-row export with its last row replaced by ``change(row)``."""
        return export([short[0], change(short[1])])

    out["good"] = export(good)
    out["empty"] = b""
    out["blank lines only"] = b"\n  \n\t\n"
    out["one event"] = export(good[:1])
    out["blank lines between rows"] = b"\n" + line(good[0]) + b"   \n\n" + line(good[1]) + line(good[2])
    out["crlf line ends"] = export(good).replace(b"\n", b"\r\n")
    out["cr line ends"] = export(good).replace(b"\n", b"\r")
    out["no final newline"] = export(good).rstrip(b"\n")
    out["starts at seq 3"] = export(good[2:])
    out["blank line of other white space"] = line(good[0]) + "\x1c\x85\u2028\u3000\n".encode() + line(good[1])
    out["line of only a byte order mark"] = line(good[0]) + "\ufeff\n".encode() + line(good[1])
    out["numbers that are not finite"] = export(
        [record(r) for r in chain([{**EVENTS[0], "after": {"x": float("nan"), "y": float("inf"), "z": float("-inf")}}])]
    )
    out["at with a space and no seconds"] = export(
        [{**r, "at": "2026-10-01 09:30+00:00"} for r in [record(c) for c in chain([{**EVENTS[0], "at": datetime(2026, 10, 1, 9, 30, tzinfo=UTC)}])]]
    )
    out["at as the end of the day before"] = export(
        [{**r, "at": "2026-09-30T24:00:00-00:00"} for r in [record(c) for c in chain([{**EVENTS[0], "at": datetime(2026, 10, 1, tzinfo=UTC)}])]]
    )
    out["at with more than six decimals"] = last(lambda r: {**r, "at": r["at"].replace(".123456", ",1234569")})
    assert out["at with more than six decimals"] != export(short)
    out["at on a day that does not exist"] = export(
        [{**r, "at": "2026-02-30T00:00:00+00:00"} for r in [record(c) for c in chain([{**EVENTS[0], "at": datetime(2026, 3, 2, tzinfo=UTC)}])]]
    )
    out["keys in another order and spaced"] = b"".join(
        json.dumps(dict(reversed(list(r.items()))), indent=None, separators=(", ", ": ")).encode() + b"\n"
        for r in good
    )
    out["at with another offset for the same instant"] = last(
        lambda r: {**r, "at": "2026-10-01T11:30:07.123456+02:00"}
    )
    out["at with Z"] = last(lambda r: {**r, "at": r["at"].replace("+00:00", "Z")})
    out["hex in capitals"] = last(lambda r: {**r, "hash": r["hash"].upper()})

    # Tampering and gaps.
    out["tampered action"] = export([good[0], {**good[1], "action": "app.deleted"}, *good[2:]])
    out["tampered after"] = export([*good[:2], {**good[2], "after": {**good[2]["after"], "cpu": 2.0}}, *good[3:]])
    out["tampered at"] = export([good[0], {**good[1], "at": "2026-10-01T09:30:08.123456+00:00"}, *good[2:]])
    out["at without an offset"] = last(lambda r: {**r, "at": r["at"].removesuffix("+00:00")})
    out["tampered canonical"] = export(
        [*good[:3], {**good[3], "canonical": base64.b64encode(b'{"forged":true}').decode()}, good[4]]
    )
    out["tampered hash"] = export([good[0], {**good[1], "hash": "ab" * 32}, *good[2:]])
    out["gap"] = export([good[0], good[1], good[3], good[4]])
    out["gap after a later start"] = export([good[2], good[4]])
    out["rows out of order"] = export([good[0], good[2], good[1]])
    out["row twice"] = export([good[0], good[1], good[1]])
    out["wrong prev_hash"] = export([good[0], good[1], {**good[2], "prev_hash": "cd" * 32}, *good[3:]])
    out["seq 1 not from genesis"] = export([{**good[0], "prev_hash": "ef" * 32}, *good[1:]])
    out["later start with any prev_hash"] = export([rehashed({**good[2], "prev_hash": "ef" * 32}, base64.b64decode(good[2]["canonical"]))])

    # Rows that are not rows.
    out["not json"] = export(good[:2]) + b"this is not json\n" + export(good[2:])
    out["a json list"] = line(good[0]) + b"[1,2,3]\n"
    out["byte order mark"] = b"\xef\xbb\xbf" + export(good)
    out["bytes that are not utf-8"] = line(good[0]) + b'{"seq":2,"at":"\xff\xfe"}\n'
    out["no canonical"] = last(lambda r: {k: v for k, v in r.items() if k != "canonical"})
    out["canonical not base64"] = last(lambda r: {**r, "canonical": r["canonical"][:-2] + "!!"})
    out["canonical without padding"] = last(lambda r: {**r, "canonical": r["canonical"].rstrip("=") + ("" if r["canonical"].endswith("=") else "A")})
    out["canonical not text"] = last(lambda r: {**r, "canonical": 5})
    out["short hash"] = last(lambda r: {**r, "hash": r["hash"][:-2]})
    out["hash not hex"] = last(lambda r: {**r, "hash": "zz" * 32})
    out["hash with spaces between bytes"] = last(lambda r: {**r, "hash": " ".join(r["hash"][i : i + 2] for i in range(0, 64, 2))})
    out["hash with a space inside a byte"] = last(lambda r: {**r, "hash": r["hash"][:1] + " " + r["hash"][1:]})
    out["prev_hash not text"] = last(lambda r: {**r, "prev_hash": None})
    out["seq as text"] = last(lambda r: {**r, "seq": str(r["seq"])})
    out["seq true"] = export([{**good[0], "seq": True}])
    out["seq missing"] = last(lambda r: {k: v for k, v in r.items() if k != "seq"})
    seq_float = line(short[1]).replace(b'"seq":2', b'"seq":2.0')
    assert seq_float != line(short[1])
    out["seq as a decimal"] = line(short[0]) + seq_float

    # Canonical bytes that hash right and are not ssc-audit-v1, or do not say what the row says.
    def canon(change: Any) -> bytes:
        return last(lambda r: rehashed(r, change(base64.b64decode(r["canonical"]))))

    out["canonical with spaces"] = canon(lambda raw: json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False).encode())
    out["canonical not sorted"] = canon(
        lambda raw: json.dumps(dict(reversed(list(json.loads(raw).items()))), separators=(",", ":"), ensure_ascii=False).encode()
    )
    out["canonical ascii escaped"] = export(
        [good[0], rehashed(good[1], json.dumps(doc(good[1]), sort_keys=True, separators=(",", ":")).encode())]
    )
    out["canonical with an extra key"] = canon(lambda raw: canonical_bytes({**json.loads(raw), "format": 2}))
    out["canonical without a key"] = canon(
        lambda raw: canonical_bytes({k: v for k, v in json.loads(raw).items() if k != "before"})
    )
    out["canonical with a key twice"] = canon(lambda raw: raw[:-1] + b',"seq":2}')
    out["canonical of another org"] = canon(
        lambda raw: canonical_bytes({**json.loads(raw), "org_id": "org_zzzzzzzzzzzzzzzzzzzz"})
    )
    out["canonical org not text"] = export([rehashed(good[0], canonical_bytes({**doc(good[0]), "org_id": 7}))])
    out["canonical of another seq"] = canon(lambda raw: canonical_bytes({**json.loads(raw), "seq": 3}))
    out["canonical seq as a decimal"] = canon(lambda raw: raw.replace(b'"seq":2', b'"seq":2.0'))
    out["canonical via_agent as 0"] = canon(
        lambda raw: canonical_bytes({**json.loads(raw), "actor": {**json.loads(raw)["actor"], "via_agent": 0}})
    )
    out["canonical actor with an extra key"] = canon(
        lambda raw: canonical_bytes({**json.loads(raw), "actor": {**json.loads(raw)["actor"], "name": "x"}})
    )
    out["canonical target a list"] = canon(lambda raw: canonical_bytes({**json.loads(raw), "target": ["org", ORG]}))
    out["canonical number written long"] = export(
        [*good[:2], rehashed(good[2], base64.b64decode(good[2]["canonical"]).replace(b'"cpu":1.0', b'"cpu":1.00'))]
    )
    out["canonical not json"] = canon(lambda raw: raw[:-1])
    out["canonical not utf-8"] = canon(lambda raw: raw.replace(b"jsonl", b"js\xffnl"))
    out["canonical a list"] = canon(lambda _raw: b"[]")
    out["row without actor"] = last(lambda r: {k: v for k, v in r.items() if k != "actor"})
    out["row before missing where canonical has null"] = last(lambda r: {k: v for k, v in r.items() if k != "before"})
    out["row client_id differs"] = last(lambda r: {**r, "actor": {**r["actor"], "client_id": "client_x"}})
    out["row target differs"] = last(lambda r: {**r, "target": {**r["target"], "id": "org_other"}})
    out["row policy decision differs"] = last(lambda r: {**r, "policy_decision_id": "pd_1"})
    out["row number in another spelling"] = export(
        [*good[:2]]
    ) + line(good[2]).replace(b'"cpu":1.0', b'"cpu":1.00').replace(b'"tiny":1e-05', b'"tiny":0.00001') + export(good[3:])
    out["row number of another type"] = export([*good[:2]]) + line(good[2]).replace(b'"cpu":1.0', b'"cpu":1') + export(good[3:])
    return out


def report(body: bytes) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "audit.jsonl"
        path.write_bytes(body)
        with path.open(encoding="utf-8", errors="replace") as lines:
            return dataclasses.asdict(check_lines(lines))


def main() -> None:
    found = {
        name: {"file": base64.b64encode(body).decode(), "report": report(body)}
        for name, body in vectors().items()
    }
    OUT.write_text(json.dumps(found, indent=0, ensure_ascii=True) + "\n", encoding="utf-8")
    for name, vector in found.items():
        r = vector["report"]
        verdict = "ok" if r["ok"] else f"{r['cause']} at line {r['broken_line']} seq {r['broken_seq']}"
        print(f"{name}: {verdict}, {r['checked']} checked, from_genesis={r['from_genesis']}")  # noqa: T201


if __name__ == "__main__":
    main()
