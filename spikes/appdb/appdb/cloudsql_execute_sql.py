"""Thin client for Cloud SQL Admin API v1 `instances.executeSql`.

Endpoint (from the discovery document, 2026-09-28):
  POST https://sqladmin.googleapis.com/v1/projects/{project}/instances/{instance}/executeSql[?location=REGION]
  body ExecuteSqlPayload: sqlStatement (required; one statement or several separated by
  semicolons), database, user, autoIamAuthn, passwordSecretVersion (regional Secret Manager
  secret in the instance's region), rowLimit, partialResultMode, application (<=32 chars).
  response SqlInstancesExecuteSqlResponse: status, metadata, messages[], results[].

Limits stated by the document:
  - result size: 10 MB (partialResultMode FAIL_PARTIAL_RESULT throws above 10 MB,
    ALLOW_PARTIAL_RESULT truncates and sets partial_result=true)
  - rowLimit: caller-set, default Unknown
  - time limit: Unknown (the ticket's "30 s" is not in the discovery doc; measure in the cloud run)
  - transaction behaviour across semicolon-separated statements: Unknown; measure
  - required IAM permission: Unknown from this doc (expected cloudsql.instances.executeSql; verify)
"""

from __future__ import annotations

import argparse
import json
import subprocess
from typing import Any

import httpx2

BASE = "https://sqladmin.googleapis.com/v1"


def build_request(
    project: str,
    instance: str,
    database: str,
    user: str | None,
    statement: str,
    *,
    location: str | None = None,
    auto_iam_authn: bool = False,
    password_secret_version: str | None = None,
    row_limit: int | None = None,
    partial_result_mode: str = "FAIL_PARTIAL_RESULT",
) -> tuple[str, dict[str, Any]]:
    url = f"{BASE}/projects/{project}/instances/{instance}/executeSql"
    if location:
        url += f"?location={location}"
    body: dict[str, Any] = {
        "sqlStatement": statement,
        "database": database,
        "partialResultMode": partial_result_mode,
        "application": "ssc-cell-agent",
    }
    if auto_iam_authn:
        body["autoIamAuthn"] = True
    elif user:
        body["user"] = user
        if password_secret_version:
            body["passwordSecretVersion"] = password_secret_version
    if row_limit is not None:
        body["rowLimit"] = str(row_limit)
    return url, body


def execute_sql(
    project: str,
    instance: str,
    database: str,
    user: str | None,
    statement: str,
    token: str,
    **kwargs: Any,
) -> dict[str, Any]:
    url, body = build_request(project, instance, database, user, statement, **kwargs)
    with httpx2.Client(timeout=60.0) as client:
        r = client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def gcloud_token() -> str:
    return subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--database", default="postgres")
    p.add_argument("--user", default=None)
    p.add_argument("--location", default=None)
    p.add_argument("--auto-iam-authn", action="store_true")
    p.add_argument("--statement", required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    url, body = build_request(
        a.project, a.instance, a.database, a.user, a.statement, location=a.location, auto_iam_authn=a.auto_iam_authn
    )
    if a.dry_run:
        print(f"POST {url}\n{json.dumps(body, indent=2)}")
        return
    print(json.dumps(execute_sql(a.project, a.instance, a.database, a.user, a.statement, gcloud_token(), location=a.location, auto_iam_authn=a.auto_iam_authn), indent=2))


if __name__ == "__main__":
    main()
