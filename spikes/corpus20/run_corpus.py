"""SSC-003 harness: pick 20 corpus apps, scan them, build with Railpack, run under the SSC-001 rules,
deploy to Cloud Run in the throwaway project, classify failures, write RESULTS.md.

Usage: uv run python run_corpus.py {select|scan|build|run|deploy|cleanup|report}
State lives in results.json next to this file. App source is copied to the scratchpad only, never here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2

HERE = Path(__file__).resolve().parent
CORPUS = Path("/Users/mihir/Documents/Internal_App_Platform/corpus-sources/corpus/artifacts")
STATE = HERE / "results.json"
SCRATCH = Path(
    os.environ.get("SSC_SCRATCH", "/private/tmp/claude-501/ssc-corpus20")
)
PROJECT = "delimitus-0926"
REGION = "us-central1"
REPO = "corpus20"
BUCKETS = [
    "needs Supabase auth",
    "uses SQLite on disk",
    "binds to localhost only",
    "runs its own in-process scheduler",
    "hard-coded key or password",
    "needs build-time public variable",
    "needs a native library",
    "other",
]

SELECTION: list[dict[str, str]] = [
    {"id": "bx-fin-bloom-dash", "cls": "builder-export", "why": "Lovable Vite+React finance dashboard with Supabase client"},
    {"id": "bx-db-buddy", "cls": "builder-export", "why": "Lovable app; corpus ingest redacted a Supabase service key in client.ts"},
    {"id": "bx-eat-what-today", "cls": "builder-export", "why": "Lovable Vite app, small, typical export"},
    {"id": "bx-secure-shopper", "cls": "builder-export", "why": "Lovable e-commerce with auth"},
    {"id": "bx-fastapi-deta-on-replit", "cls": "builder-export", "why": "Replit Python FastAPI, requirements.txt"},
    {"id": "bx-freecodecamp-project-exercise-tracker", "cls": "builder-export", "why": "Replit Node/Express with Mongo URL in sample.env"},
    {"id": "bx-my-rest-api", "cls": "builder-export", "why": "Replit Node API with a Dockerfile and a redacted Google key"},
    {"id": "bx-clinical-evidence-synthesizer", "cls": "builder-export", "why": "Replit Python data app"},
    {"id": "ar-flask-expense-app", "cls": "agent-repo", "why": "Flask app, SQLite likely"},
    {"id": "ar-budget-tracker", "cls": "agent-repo", "why": "Next.js with Prisma and middleware"},
    {"id": "ar-habit-tracker", "cls": "agent-repo", "why": "Node/Express with env.js and vercel.json"},
    {"id": "ar-ohmytodolist", "cls": "agent-repo", "why": "Cursor-built Vite SPA with prebuilt dist"},
    {"id": "ar-draw-a-ui", "cls": "agent-repo", "why": "Copilot-built Next.js app calling OpenAI"},
    {"id": "ar-nutri-agent-bot", "cls": "agent-repo", "why": "Cursor-built Python bot with database dir"},
    {"id": "ar-broken-link-website", "cls": "agent-repo", "why": "Lovable-marked repo with Dockerfile and AGENTS.md"},
    {"id": "ar-poi-extraction-tool", "cls": "agent-repo", "why": "Cursor-built Python tool with on-disk cache"},
    {"id": "ab-attend-ops", "cls": "arbitrary", "why": "docker-compose frontend+backend, Lovable-marked"},
    {"id": "ab-cloudy-the-discord-bot", "cls": "arbitrary", "why": "Replit Python bot, poetry, no HTTP server"},
    {"id": "ab-codesphinx-freshlaundry-withadmin-and-monthlypla", "cls": "arbitrary", "why": "Lovable SaaS with supabase/ dir"},
    {"id": "ab-flat-calculator", "cls": "arbitrary", "why": "static site with dist/ only"},
]

SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "dist", "build", "__pycache__", ".next"}
LOCKFILES = {"package-lock.json", "yarn.lock", "bun.lockb", "pnpm-lock.yaml", "poetry.lock", "uv.lock"}
MANIFESTS = {"package.json", "requirements.txt", "pyproject.toml", "Pipfile"}
TEXT_EXT = {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".py", ".json", ".toml", ".env", ".txt",
            ".yml", ".yaml", ".html", ".md", ".cfg", ".ini", ".sh", ".ejs", ".vue", ""}
SIGNALS = {
    "supabase": re.compile(r"@supabase/supabase-js|supabase\.auth|from supabase|createClient\(", re.I),
    "sqlite": re.compile(r"\bsqlite3\b|better-sqlite3|\.sqlite3?\b|\.db['\"]|sqlite://", re.I),
    "localhost_bind": re.compile(r"(listen|run|bind|host)\s*\(?[^\n]{0,40}(127\.0\.0\.1|['\"]localhost['\"])", re.I),
    "scheduler": re.compile(r"node-cron|APScheduler|import schedule\b|BackgroundScheduler|cron\.schedule|setInterval\([^)]*\d{5,}", re.I),
    "build_public_var": re.compile(r"\bVITE_[A-Z0-9_]+|\bNEXT_PUBLIC_[A-Z0-9_]+|REACT_APP_[A-Z0-9_]+", re.I),
    "secret_like": re.compile(r"\bsk-[A-Za-z0-9]{10,}|\bsk_(live|test)_[A-Za-z0-9]{6,}|\beyJ[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{12,}|password\s*=\s*['\"][^'\"]{4,}['\"]", re.I),
    "native": re.compile(r"[\"']sharp[\"']|[\"']canvas[\"']|[\"']bcrypt[\"']|node-gyp|psycopg2(?!-binary)|\bpuppeteer\b|playwright|opencv|torch\b|tensorflow", re.I),
    "port_env": re.compile(r"process\.env\.PORT|os\.environ(\.get)?\(?\[?['\"]PORT|getenv\(['\"]PORT", re.I),
    "hard_port": re.compile(r"(listen|run|port)\s*[(=:]\s*(\d{4,5})\b", re.I),
    "health_path": re.compile(r"['\"]/(health|healthz|ping|status)['\"]", re.I),
}


def load() -> dict[str, Any]:
    return json.loads(STATE.read_text()) if STATE.exists() else {"apps": {}}


def save(st: dict[str, Any]) -> None:
    STATE.write_text(json.dumps(st, indent=2, sort_keys=True))


def bundle(app: dict[str, Any]) -> Path:
    return CORPUS / app["cls"] / app["id"] / "bundle"


def redact(line: str) -> str:
    return SIGNALS["secret_like"].sub("<redacted>", line)


def sh(cmd: list[str], timeout: float = 900.0, **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def stack_guess(b: Path) -> str:
    if (b / "Dockerfile").exists():
        return "dockerfile"
    pj = b / "package.json"
    if pj.exists():
        try:
            d = json.loads(pj.read_text())
        except ValueError:
            return "node (bad package.json)"
        deps = {**d.get("dependencies", {}), **d.get("devDependencies", {})}
        for k, name in (("next", "next.js"), ("vite", "vite spa"), ("express", "express"), ("react-scripts", "cra")):
            if k in deps:
                return name
        return "node (" + ",".join(d.get("scripts", {}).keys())[:40] + ")"
    if (b / "requirements.txt").exists() or (b / "pyproject.toml").exists():
        txt = " ".join(p.read_text(errors="ignore") for p in (b / "requirements.txt", b / "pyproject.toml") if p.exists()).lower()
        for k in ("fastapi", "flask", "streamlit", "django", "discord"):
            if k in txt:
                return f"python {k}"
        return "python"
    if (b / "index.html").exists() or (b / "dist" / "index.html").exists():
        return "static html"
    return "unknown"


def do_select(st: dict[str, Any]) -> None:
    out = []
    for s in SELECTION:
        b = bundle(s)
        origin = json.loads((b.parent / "origin.json").read_text())
        app = {**s, "builder_source": origin.get("builder", "unknown"), "stack": stack_guess(b),
               "repo": origin.get("sourceRepo", {}).get("url")}
        out.append(app)
        st["apps"].setdefault(s["id"], {}).update(app)
    (HERE / "selection.json").write_text(json.dumps(out, indent=2))
    save(st)
    print(f"selected {len(out)}")


def do_scan(st: dict[str, Any]) -> None:
    for app in st["apps"].values():
        b = bundle(app)
        hits: dict[str, int] = {k: 0 for k in SIGNALS}
        files = 0
        for p in b.rglob("*"):
            if any(part in SKIP_DIRS for part in p.relative_to(b).parts) or not p.is_file():
                continue
            if p.suffix not in TEXT_EXT or p.stat().st_size > 2_000_000 or p.name in LOCKFILES:
                continue
            files += 1
            try:
                txt = p.read_text(errors="ignore")
            except OSError:
                continue
            for k, rx in SIGNALS.items():
                if k == "native" and p.name not in MANIFESTS:
                    continue
                hits[k] += len(rx.findall(txt))
        latent = []
        if hits["supabase"]:
            latent.append("needs Supabase auth")
        if hits["sqlite"]:
            latent.append("uses SQLite on disk")
        if hits["localhost_bind"]:
            latent.append("binds to localhost only")
        if hits["scheduler"]:
            latent.append("runs its own in-process scheduler")
        if hits["secret_like"]:
            latent.append("hard-coded key or password")
        if hits["build_public_var"]:
            latent.append("needs build-time public variable")
        if hits["native"]:
            latent.append("needs a native library")
        app["scan"] = {"files_scanned": files, "hits": hits, "dockerfile": (b / "Dockerfile").exists(),
                       "port_env": bool(hits["port_env"]), "hard_port": bool(hits["hard_port"]),
                       "health_path": bool(hits["health_path"]), "latent": latent}
        print(app["id"], app["stack"], latent)
    save(st)


def do_build(st: dict[str, Any]) -> None:
    if shutil.which("railpack") is None:
        sys.exit("railpack not installed; install it, then rerun build")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for app in st["apps"].values():
        if app.get("build", {}).get("ok"):
            continue
        src, dst = bundle(app), SCRATCH / app["id"]
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=True)
        t0 = time.perf_counter()
        r = sh(["railpack", "build", str(dst), "--name", f"corpus20/{app['id']}", "--platform", "linux/amd64",
                "--progress", "plain"], timeout=1200)
        app["build"] = {"ok": r.returncode == 0, "seconds": round(time.perf_counter() - t0, 1),
                        "tail": [redact(x) for x in (r.stdout + r.stderr).splitlines()[-40:]]}
        print(app["id"], "built" if r.returncode == 0 else "BUILD FAILED", app["build"]["seconds"])
        save(st)


def probe(cid: str) -> dict[str, Any]:
    """Probe from inside the container's network namespace: --network none drops published ports."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 60:
        st = sh(["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", cid]).stdout.split()
        if st and st[0] != "running":
            return {"status": None, "exit_code": int(st[1]), "seconds": round(time.perf_counter() - t0, 1)}
        r = sh(["docker", "run", "--rm", f"--network=container:{cid}", "curlimages/curl", "-s", "-o", "/dev/null",
                "-m", "3", "-w", "%{http_code}", "http://127.0.0.1:8080/"])
        code = r.stdout.strip()
        if r.returncode == 0 and code.isdigit() and int(code) > 0:
            return {"status": int(code), "exit_code": None, "seconds": round(time.perf_counter() - t0, 1)}
        time.sleep(1)
    return {"status": None, "exit_code": None, "seconds": 60.0, "timeout": True}


def do_run(st: dict[str, Any]) -> None:
    for app in st["apps"].values():
        if not app.get("build", {}).get("ok"):
            app["run"] = {"skipped": "not built"}
            continue
        img = f"corpus20/{app['id']}"
        base = ["docker", "run", "-d", "--platform", "linux/amd64", "--user", "10001:10001", "--tmpfs", "/tmp",
                "-e", "PORT=8080", "--network", "none"]
        for read_only in (True, False):
            cmd = base + (["--read-only"] if read_only else []) + [img]
            r = sh(cmd)
            if r.returncode != 0:
                app["run"] = {"ok": False, "read_only": read_only, "error": redact(r.stderr[-400:])}
                break
            cid = r.stdout.strip()
            res = probe(cid)
            lg = sh(["docker", "logs", cid])
            logs = (lg.stdout + lg.stderr).splitlines()[:40]
            sh(["docker", "rm", "-f", cid])
            res.update(ok=res["status"] is not None, read_only=read_only, logs=[redact(x) for x in logs])
            app["run"] = res
            if res["ok"] or res.get("exit_code") is None:
                break
        print(app["id"], {k: v for k, v in app["run"].items() if k != "logs"})
        save(st)


def do_deploy(st: dict[str, Any]) -> None:
    reg = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPO}"
    if REPO not in sh(["gcloud", "artifacts", "repositories", "list", f"--project={PROJECT}", f"--location={REGION}", "--format=value(name)"]).stdout:
        r = sh(["gcloud", "artifacts", "repositories", "create", REPO, "--repository-format=docker", f"--location={REGION}", f"--project={PROJECT}"])
        if r.returncode:
            sys.exit(r.stderr)
    sh(["gcloud", "auth", "configure-docker", f"{REGION}-docker.pkg.dev", "--quiet"])
    for n, app in enumerate(st["apps"].values(), 1):
        if not app.get("build", {}).get("ok"):
            app["cloudrun"] = {"skipped": "not built"}
            continue
        img = f"{reg}/{app['id']}:spike"
        sh(["docker", "tag", f"corpus20/{app['id']}", img])
        p = sh(["docker", "push", img], timeout=1200)
        if p.returncode:
            app["cloudrun"] = {"ok": False, "stage": "push", "error": p.stderr[-400:]}
            continue
        svc = f"corpus20-{n}"
        t0 = time.perf_counter()
        d = sh(["gcloud", "run", "deploy", svc, f"--image={img}", f"--region={REGION}", f"--project={PROJECT}",
                "--no-allow-unauthenticated", "--ingress=internal", "--port=8080", "--cpu=1", "--memory=512Mi",
                "--max-instances=1", "--timeout=60", "--quiet"], timeout=900)
        app["cloudrun"] = {"ok": d.returncode == 0, "service": svc, "deploy_seconds": round(time.perf_counter() - t0, 1),
                           "tail": [redact(x) for x in (d.stdout + d.stderr).splitlines()[-25:]]}
        print(svc, app["id"], "ready" if d.returncode == 0 else "NOT READY")
        save(st)


def do_cleanup(st: dict[str, Any]) -> None:
    for app in st["apps"].values():
        svc = app.get("cloudrun", {}).get("service")
        if svc:
            sh(["gcloud", "run", "services", "delete", svc, f"--region={REGION}", f"--project={PROJECT}", "--quiet"])
    sh(["gcloud", "artifacts", "repositories", "delete", REPO, f"--location={REGION}", f"--project={PROJECT}", "--quiet"])
    svcs = sh(["gcloud", "run", "services", "list", f"--project={PROJECT}", "--format=value(name)"]).stdout.strip()
    repos = sh(["gcloud", "artifacts", "repositories", "list", f"--project={PROJECT}", "--format=value(name)"]).stdout.strip()
    st["cleanup"] = {"services_left": svcs.splitlines(), "repos_left": repos.splitlines()}
    save(st)
    print("services left:", svcs or "none", "| repos left:", repos or "none")


def classify(app: dict[str, Any]) -> tuple[str, str]:
    """Return (outcome, bucket). Runtime evidence first, then static signals."""
    scan, build, run = app.get("scan", {}), app.get("build"), app.get("run")
    latent = scan.get("latent", [])
    if build is None or run is None:
        return "pending railpack", latent[0] if latent else "-"
    text = "\n".join(build.get("tail", []) + run.get("logs", []) + [run.get("error", "")]).lower()
    if not build["ok"]:
        if "vite_" in text or "next_public_" in text:
            return "build failed", "needs build-time public variable"
        if any(k in text for k in ("node-gyp", "gyp err", "sharp", "canvas", "gcc", "cmake")):
            return "build failed", "needs a native library"
        return "build failed", "other: " + next((line for line in build["tail"] if "error" in line.lower()), "build error")[:100]
    if run.get("ok"):
        return "runs", ("runs but: " + ", ".join(latent)) if latent else "-"
    if "supabase" in text:
        return "fails at run", "needs Supabase auth"
    if "sqlite" in text or "readonly database" in text or "unable to open database" in text:
        return "fails at run", "uses SQLite on disk"
    if run.get("timeout") and scan.get("hard_port") and not scan.get("port_env"):
        return "fails at run", "binds to localhost only"
    for b in latent:
        if b in ("needs Supabase auth", "uses SQLite on disk", "binds to localhost only", "hard-coded key or password"):
            return "fails at run", b
    return "fails at run", "other: " + (run.get("error") or next((line for line in run.get("logs", []) if "error" in line.lower()), "no HTTP answer in 60 s"))[:100]


def do_report(st: dict[str, Any]) -> None:
    rows, fail_counts, latent_counts, runs_but = [], {}, {}, {}
    for app in st["apps"].values():
        outcome, bucket = classify(app)
        app["outcome"], app["bucket"] = outcome, bucket
        for b in app.get("scan", {}).get("latent", []):
            latent_counts[b] = latent_counts.get(b, 0) + 1
        if outcome in ("build failed", "fails at run"):
            key = bucket.split(":")[0]
            fail_counts[key] = fail_counts.get(key, 0) + 1
        if outcome == "runs" and bucket != "-":
            for b in bucket.removeprefix("runs but: ").split(", "):
                runs_but[b] = runs_but.get(b, 0) + 1
        s = app.get("scan", {})
        rows.append(f"| {app['id']} | {app['cls']} | {app['builder_source']} | {app['stack']} | {', '.join(s.get('latent', [])) or '-'} | "
                    f"{fmt(app.get('build'))} | {fmt(app.get('run'))} | {fmt(app.get('cloudrun'))} | {outcome} | {bucket} |")
    save(st)
    table = ("| id | class | source | stack | latent buckets (static) | built | started locally | Cloud Run ready | outcome | final bucket |\n"
             "|---|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows))
    ranked = lambda d: "\n".join(f"| {k} | {v} |" for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or "| none yet | 0 |"
    (HERE / "TABLES.md").write_text(
        "## Twenty apps\n\n" + table + "\n\n## Static-scan latent buckets (n=20)\n\n| bucket | apps |\n|---|---|\n" + ranked(latent_counts)
        + "\n\n## Runtime failures\n\n| bucket | apps |\n|---|---|\n" + ranked(fail_counts)
        + "\n\n## Runs but carries a latent problem\n\n| bucket | apps |\n|---|---|\n" + ranked(runs_but) + "\n")
    print("wrote TABLES.md; paste into RESULTS.md")


def fmt(x: dict[str, Any] | None) -> str:
    if x is None:
        return "pending railpack"
    if "skipped" in x:
        return "skipped"
    return "yes" if x.get("ok") else "no"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["select", "scan", "build", "run", "deploy", "cleanup", "report"])
    a = ap.parse_args()
    st = load()
    {"select": do_select, "scan": do_scan, "build": do_build, "run": do_run,
     "deploy": do_deploy, "cleanup": do_cleanup, "report": do_report}[a.cmd](st)


if __name__ == "__main__":
    main()
