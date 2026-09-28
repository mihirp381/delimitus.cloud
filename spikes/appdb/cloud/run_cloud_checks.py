"""SSC-005 cloud half: create app databases on Cloud SQL as the cell-agent identity, prove a no-role
identity is refused, measure executeSql limits, and prove cross-connect is refused on the real instance.

Usage: uv run python cloud/run_cloud_checks.py --project P --instance I --region R --host IP
Writes cloud/RESULTS.json (no passwords). Never prints tokens.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx2
import psycopg

from appdb.cloudsql_execute_sql import build_request

BASE = "https://sqladmin.googleapis.com/v1"


def token_for(sa: str) -> str:
    return subprocess.run(
        ["gcloud", "auth", "print-access-token", f"--impersonate-service-account={sa}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def call(token: str, url: str, body: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        with httpx2.Client(timeout=timeout) as c:
            r = c.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        try:
            payload = r.json()
        except ValueError:
            payload = {"raw": r.text[:500]}
        return {"http": r.status_code, "elapsed_s": round(time.perf_counter() - t0, 2), "payload": payload}
    except httpx2.HTTPError as e:
        return {"http": None, "elapsed_s": round(time.perf_counter() - t0, 2), "error": f"{type(e).__name__}: {e}"}


def summarize(res: dict[str, Any]) -> dict[str, Any]:
    p = res.get("payload", {})
    out: dict[str, Any] = {"http": res.get("http"), "elapsed_s": res.get("elapsed_s")}
    if "error" in res:
        out["error"] = res["error"]
    if isinstance(p, dict):
        if "error" in p:
            out["api_error"] = {"code": p["error"].get("code"), "message": p["error"].get("message", "")[:300]}
        if isinstance(p.get("status"), dict) and p["status"].get("code"):
            out["sql_error"] = p["status"].get("message", "")[:300]
        if "results" in p:
            out["rows"] = sum(len(r.get("rows", [])) for r in p["results"])
    out["ok"] = out["http"] == 200 and "api_error" not in out and "sql_error" not in out
    return out


def wait_op(token: str, project: str, name: str, timeout_s: float = 300.0) -> float:
    t0 = time.perf_counter()
    with httpx2.Client(timeout=30.0) as c:
        while time.perf_counter() - t0 < timeout_s:
            r = c.get(f"{BASE}/projects/{project}/operations/{name}", headers={"Authorization": f"Bearer {token}"})
            if r.json().get("status") == "DONE":
                return round(time.perf_counter() - t0, 2)
            time.sleep(1)
    raise TimeoutError(name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--instance", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--host", required=True, help="instance public IP, authorised for this machine only")
    ap.add_argument("--count", type=int, default=10)
    a = ap.parse_args()
    cell = f"cell-agent@{a.project}.iam.gserviceaccount.com"
    null = f"null-identity@{a.project}.iam.gserviceaccount.com"
    cell_tok, null_tok = token_for(cell), token_for(null)
    results: dict[str, Any] = {"project": a.project, "instance": a.instance, "checks": {}}
    ck = results["checks"]
    sup = "SET ROLE cloudsqlsuperuser; "

    def sql(statement: str, database: str = "postgres", tok: str = cell_tok, **kw: Any) -> dict[str, Any]:
        timeout = kw.pop("timeout", 120.0)
        url, body = build_request(a.project, a.instance, database, None, statement, location=a.region, auto_iam_authn=True, **kw)
        return summarize(call(tok, url, body, timeout=timeout))

    def create_db(name: str, tok: str) -> dict[str, Any]:
        r = call(tok, f"{BASE}/projects/{a.project}/instances/{a.instance}/databases", {"name": name}, timeout=60.0)
        s = summarize(r)
        if s["http"] == 200:
            s["op_s"] = wait_op(tok, a.project, r["payload"]["name"])
        return s

    ck["cell_agent_can_call_execute_sql"] = sql("select current_user")
    ck["null_identity_execute_sql_refused"] = sql("select current_user", tok=null_tok)
    ck["null_identity_create_database_refused"] = create_db("zz_null_should_fail", null_tok)

    created: list[tuple[str, str]] = []
    steps: list[dict[str, Any]] = []
    for _ in range(a.count):
        role = f"app_{secrets.token_hex(4)}"
        pw = secrets.token_urlsafe(24)
        t0 = time.perf_counter()
        s1 = sql(f"{sup}CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 20 PASSWORD '{pw}'")
        s2 = create_db(role, cell_tok) if s1["ok"] else {"ok": False, "skipped": True}
        s3 = sql(f"{sup}REVOKE ALL ON SCHEMA public FROM PUBLIC; GRANT ALL ON SCHEMA public TO {role}", database=role) if s2["ok"] else {"ok": False, "skipped": True}
        s4 = sql(f"{sup}GRANT {role} TO cloudsqlsuperuser; ALTER DATABASE {role} OWNER TO {role}; REVOKE CONNECT ON DATABASE {role} FROM PUBLIC; REVOKE {role} FROM cloudsqlsuperuser") if s3["ok"] else {"ok": False, "skipped": True}
        steps.append({"role": role, "total_s": round(time.perf_counter() - t0, 2), "create_role": s1, "create_db": s2, "schema": s3, "owner_revoke": s4})
        if s4["ok"]:
            created.append((role, pw))
    ck["create_10_databases"] = {
        "attempted": a.count,
        "succeeded": len(created),
        "total_s_each": [s["total_s"] for s in steps],
        "first_failure": next((s for s in steps if not s["owner_revoke"].get("ok")), None),
    }
    ck["databases_visible"] = sql("select count(*) from pg_database where datname like 'app_%'")

    if len(created) >= 2:
        (ra, pa), (rb, _) = created[0], created[1]
        def connect(user: str, pw: str, db: str) -> str:
            try:
                with psycopg.connect(host=a.host, port=5432, dbname=db, user=user, password=pw, sslmode="require", connect_timeout=15) as c:
                    return "connected: " + str(c.execute("select current_database()").fetchone()[0])
            except psycopg.Error as e:
                return f"refused: {type(e).__name__}: {str(e).strip()[:160]}"
        ck["own_db_connect"] = connect(ra, pa, ra)
        ck["cross_db_connect"] = connect(ra, pa, rb)
        ck["own_db_create_table"] = None
        try:
            with psycopg.connect(host=a.host, port=5432, dbname=ra, user=ra, password=pa, sslmode="require", connect_timeout=15) as c:
                c.execute("create table t(x int)")
                ck["own_db_create_table"] = "ok"
        except psycopg.Error as e:
            ck["own_db_create_table"] = f"{type(e).__name__}: {str(e).strip()[:160]}"

    ck["multi_statement_is_one_transaction"] = sql(f"{sup}CREATE ROLE zz_tx LOGIN PASSWORD 'zzTemp{secrets.token_hex(8)}'; select 1/0")
    ck["role_from_aborted_batch_exists"] = sql("select rolname from pg_roles where rolname='zz_tx'")
    ck["create_database_in_batch"] = sql(f"{sup}CREATE DATABASE zz_batch")
    ck["statement_40s"] = sql("select pg_sleep(40), 1", timeout=180.0)
    ck["statement_20s"] = sql("select pg_sleep(20), 1", timeout=180.0)
    ck["result_25mb_fail_partial"] = sql("select repeat('x', 1000) as c from generate_series(1, 25000)", timeout=180.0)
    ck["result_25mb_allow_partial"] = sql("select repeat('x', 1000) as c from generate_series(1, 25000)", timeout=180.0, partial_result_mode="ALLOW_PARTIAL_RESULT")

    sql(f"{sup}DROP ROLE IF EXISTS zz_tx; DROP ROLE IF EXISTS zz_role")
    results["app_roles"] = [r for r, _ in created]
    Path(__file__).with_name("RESULTS.json").write_text(json.dumps(results, indent=2, default=str))
    for k, v in ck.items():
        print(k, json.dumps(v, default=str)[:300])


if __name__ == "__main__":
    main()
