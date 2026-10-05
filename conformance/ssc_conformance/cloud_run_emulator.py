"""An in-memory Cloud Run Admin API v2 and IAM service-account API, for ``httpx2.MockTransport``.

It models what ``CloudRunDriver`` relies on, pessimistically where Cloud Run's behaviour is not
documented: a write leaves the service reconciling, and its revision does not exist yet, until
``settle()``; ``LATEST`` traffic statuses name no revision; traffic may only name revisions that
exist; an ``etag`` that is not the current one is refused. ``unhealthy(digest)`` makes revisions
of that image fail; ``index(digest, platform)`` makes revisions of that image index run, and
report, its platform manifest, as Cloud Run does.
"""

import copy
import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, cast

import httpx2

type Json = dict[str, Any]

PROJECT: Final = "ssc-c-emulated"
REGION: Final = "us-central1"
_LATEST: Final = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
_REVISION: Final = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
RESERVED_ENV: Final = frozenset({"PORT", "K_SERVICE", "K_REVISION", "K_CONFIGURATION"})


@dataclass
class _Service:
    name: str
    body: Json
    generation: int = 1
    observed: int = 0
    etag_n: int = 1
    revisions: list[Json] = field(default_factory=list[Json])
    pending_revision: Json | None = None
    traffic_statuses: list[Json] = field(default_factory=list[Json])
    policy: Json | None = None


class CloudRunEmulator:
    def __init__(self, *, auto_settle: bool = False) -> None:
        self.services: dict[str, _Service] = {}
        self.accounts: dict[str, Json] = {}
        self.calls: list[tuple[str, str]] = []
        self.auto_settle = auto_settle
        self._unhealthy: set[str] = set()
        self._indexes: dict[str, str] = {}
        self._clock = itertools.count(1)
        self.unusable_account_creates = 0

    # ── test controls ────────────────────────────────────────────────────────

    def new_accounts_unusable_for(self, creates: int) -> None:
        """Refuse the next ``creates`` service creates as IAM does while a new account settles."""
        self.unusable_account_creates = creates

    def unhealthy(self, digest: str) -> None:
        self._unhealthy.add(digest)

    def index(self, digest: str, platform: str) -> None:
        self._indexes[digest] = platform

    def settle(self) -> None:
        for svc in self.services.values():
            self._settle(svc)

    def edit(self, service: str, change: Callable[[Json], None]) -> None:
        """A change made outside SSC (the console, gcloud): ``change(body)`` edits the service
        body in place, then it is written as a PATCH would be."""
        svc = self.services[service]
        body = copy.deepcopy(svc.body)
        change(body)
        self._write(svc, body)
        self._settle(svc)

    def policy(self, service: str) -> Json | None:
        return self.services[service].policy

    # ── transport ────────────────────────────────────────────────────────────

    def handler(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911  (one per route)
        path = request.url.path
        self.calls.append((request.method, path))
        body = cast(Json, json.loads(request.content)) if request.content else {}
        host = request.url.host
        if host == "iam.googleapis.com":
            return self._iam(request.method, path, body)
        if host != "run.googleapis.com":
            return _error(404, "NOT_FOUND", f"no host {host}")
        parent = f"/v2/projects/{PROJECT}/locations/{REGION}/services"
        if not path.startswith(parent):
            return _error(403, "PERMISSION_DENIED", "outside the emulated cell")
        rest = path.removeprefix(parent).strip("/")
        if not rest and request.method == "POST":
            return self._create(request.url.params.get("serviceId", ""), body)
        name, _, tail = rest.partition("/")
        name, _, verb = name.partition(":")
        svc = self.services.get(name)
        if svc is None:
            return _error(404, "NOT_FOUND", f"service {name} not found")
        match request.method, tail, verb:
            case "GET", "", "":
                return httpx2.Response(200, json=self._view(svc))
            case "PATCH", "", "":
                return self._patch(svc, body)
            case "GET", "revisions", "":
                return httpx2.Response(200, json={"revisions": copy.deepcopy(svc.revisions)})
            case "POST", "", "setIamPolicy":
                svc.policy = copy.deepcopy(body["policy"])
                return httpx2.Response(200, json=svc.policy)
            case _:
                return _error(404, "NOT_FOUND", f"{request.method} {path}")

    # ── IAM ──────────────────────────────────────────────────────────────────

    def _iam(self, method: str, path: str, body: Json) -> httpx2.Response:
        if method != "POST" or path != f"/v1/projects/{PROJECT}/serviceAccounts":
            return _error(404, "NOT_FOUND", path)
        account = str(body["accountId"])
        if account in self.accounts:
            return _error(409, "ALREADY_EXISTS", f"{account} exists")
        email = f"{account}@{PROJECT}.iam.gserviceaccount.com"
        self.accounts[account] = {"email": email, **body["serviceAccount"]}
        return httpx2.Response(200, json=self.accounts[account])

    # ── services ─────────────────────────────────────────────────────────────

    def _create(self, name: str, body: Json) -> httpx2.Response:
        if name in self.services:
            return _error(409, "ALREADY_EXISTS", f"service {name} exists")
        refused = self._refuse_template(name, body.get("template") or {})
        if refused:
            return refused
        if self.unusable_account_creates:
            self.unusable_account_creates -= 1
            account = body["template"]["serviceAccount"]
            return _error(
                403,
                "PERMISSION_DENIED",
                f"Permission 'iam.serviceaccounts.actAs' denied on service account {account}"
                " (or it may not exist).",
            )
        body = copy.deepcopy(body)
        body.setdefault("traffic", [{"type": _LATEST, "percent": 100}])
        svc = _Service(name=name, body=body)
        svc.pending_revision = self._revision(name, body["template"], 1)
        self.services[name] = svc
        if self.auto_settle:
            self._settle(svc)
        return httpx2.Response(200, json={"name": "operations/create", "done": False})

    def _patch(self, svc: _Service, body: Json) -> httpx2.Response:
        if body.get("etag") not in (None, self._etag(svc)):
            return _error(409, "ABORTED", "etag mismatch")
        refused = self._refuse_template(svc.name, body.get("template") or {})
        if refused:
            return refused
        known = {r["name"].rsplit("/", 1)[-1] for r in svc.revisions}
        changes_template = _template_key(body.get("template")) != _template_key(
            svc.body.get("template")
        )
        new_name = (body.get("template") or {}).get("revision")
        if changes_template and new_name in known:
            return _error(409, "ALREADY_EXISTS", f"revision {new_name} exists")
        if changes_template and new_name:
            known.add(new_name)
        for target in body.get("traffic") or []:
            if target.get("type") == _REVISION and target.get("revision") not in known:
                return _error(400, "INVALID_ARGUMENT", f"revision {target.get('revision')}")
        self._write(svc, body)
        if self.auto_settle:
            self._settle(svc)
        return httpx2.Response(200, json={"name": "operations/update", "done": False})

    def _write(self, svc: _Service, body: Json) -> None:
        old_template = svc.body.get("template")
        svc.body = {k: copy.deepcopy(v) for k, v in body.items() if k != "etag"}
        svc.generation += 1
        svc.etag_n += 1
        if _template_key(svc.body.get("template")) != _template_key(old_template):
            svc.pending_revision = self._revision(svc.name, svc.body["template"], svc.generation)

    def _refuse_template(self, service: str, template: Json) -> httpx2.Response | None:
        account = str(template.get("serviceAccount") or "").split("@", 1)[0]
        if account not in self.accounts:
            return _error(400, "INVALID_ARGUMENT", f"service account {account} does not exist")
        reserved = [
            str(var.get("name"))
            for container in template.get("containers") or []
            for var in container.get("env") or []
            if var.get("name") in RESERVED_ENV
        ]
        if reserved:
            return _error(400, "INVALID_ARGUMENT", f"reserved env names: {', '.join(reserved)}")
        revision = template.get("revision")
        if revision is not None and not str(revision).startswith(service + "-"):
            return _error(400, "INVALID_ARGUMENT", "revision names start with the service name")
        return None

    def _revision(self, service: str, template: Json, generation: int) -> Json:
        short = template.get("revision") or f"{service}-{generation:05d}-auto"
        revision = copy.deepcopy(template)
        revision.pop("revision", None)
        path = f"projects/{PROJECT}/locations/{REGION}/services/{service}"
        revision["name"] = f"{path}/revisions/{short}"
        revision["createTime"] = f"2026-09-30T00:00:{next(self._clock):05d}Z"
        container = revision["containers"][0]
        repository, _, digest = str(container["image"]).rpartition("@")
        if digest in self._indexes:
            container["image"] = f"{repository}@{self._indexes[digest]}"
        failed = digest in self._unhealthy
        state = "CONDITION_FAILED" if failed else "CONDITION_SUCCEEDED"
        revision["conditions"] = [{"type": "Ready", "state": state}]
        return revision

    def _settle(self, svc: _Service) -> None:
        if svc.pending_revision is not None:
            svc.revisions.append(svc.pending_revision)
            svc.pending_revision = None
        svc.observed = svc.generation
        svc.traffic_statuses = [
            {k: v for k, v in t.items() if k in {"type", "revision", "percent"}}
            for t in svc.body.get("traffic") or []
        ]

    def _etag(self, svc: _Service) -> str:
        return f'"{svc.etag_n}"'

    def _view(self, svc: _Service) -> Json:
        view = copy.deepcopy(svc.body)
        ready = [r for r in svc.revisions if r["conditions"][0]["state"] == "CONDITION_SUCCEEDED"]
        view |= {
            "name": f"projects/{PROJECT}/locations/{REGION}/services/{svc.name}",
            "generation": str(svc.generation),
            "observedGeneration": str(svc.observed),
            "reconciling": svc.observed < svc.generation,
            "etag": self._etag(svc),
            "trafficStatuses": copy.deepcopy(svc.traffic_statuses),
        }
        if svc.revisions:
            view["latestCreatedRevision"] = svc.revisions[-1]["name"]
        if ready:
            view["latestReadyRevision"] = ready[-1]["name"]
        scaling = view.get("scaling") or {}
        view["scaling"] = {k: v for k, v in scaling.items() if v not in (0, None)}
        return view


def _template_key(template: Json | None) -> str:
    return json.dumps(template or {}, sort_keys=True)


def _error(status: int, reason: str, message: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"code": status, "status": reason, "message": message}}
    )
