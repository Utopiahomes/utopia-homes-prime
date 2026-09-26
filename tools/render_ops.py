"""Deploy Utopia services on Render and set their environment variables.

Uses the Render CLI's stored sign-in (~/.render/cli.yaml) and never prints the API key or any
variable's value. Only the services listed below can be touched.

    python tools/render_ops.py deploy homes-prime [--commit SHA]
    python tools/render_ops.py set-env homes-prime NAME=VALUE [NAME=VALUE ...]
    python tools/render_ops.py status homes-prime
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SERVICES = {
    "homes-prime": "srv-daqnmhrncjis739cuepg",
    "lucy": "srv-darvbonavr4c738c62b0",
}
# Variables that hold secrets are set in the Render dashboard, never from here.
_SECRET = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL|PEM)", re.IGNORECASE)


def _api_key() -> str:
    config = (Path.home() / ".render" / "cli.yaml").read_text(encoding="utf-8")
    match = re.search(r"^api:\s*\n(?:\s+.*\n)*?\s+key:\s*(\S+)", config, re.MULTILINE)
    if not match:
        sys.exit("no Render API key in ~/.render/cli.yaml; run `render login` first")
    return match.group(1)


def _call(method: str, path: str, body: Any = None) -> Any:
    request = urllib.request.Request(
        f"https://api.render.com/v1{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        sys.exit(f"Render API {method} {path}: {exc.code} {detail}")


def _latest_deploy(service: str) -> dict[str, Any]:
    deploys = _call("GET", f"/services/{service}/deploys?limit=1")
    return deploys[0].get("deploy", deploys[0])


def status(service: str) -> None:
    d = _latest_deploy(service)
    print(d["status"], d.get("commit", {}).get("id", "")[:7], d.get("finishedAt"))


def deploy(service: str, commit: str | None, wait: bool) -> None:
    commit = (
        commit or subprocess.check_output(["git", "rev-parse", "origin/main"], text=True).strip()
    )
    d = _call("POST", f"/services/{service}/deploys", {"commitId": commit})
    print("deploy", d.get("id"), "for", commit[:7])
    while wait:
        time.sleep(15)
        d = _latest_deploy(service)
        if d["status"] not in ("created", "queued", "build_in_progress", "update_in_progress",
                               "pre_deploy_in_progress"):  # fmt: skip
            print(d["status"], d.get("commit", {}).get("id", "")[:7])
            sys.exit(0 if d["status"] == "live" else 1)


def set_env(service: str, pairs: list[str]) -> None:
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            sys.exit(f"expected NAME=VALUE, got {pair!r}")
        if re.match(r"[A-Za-z]:[/\\]", value):
            sys.exit(f"{name} is a Windows path ({value}); in Git Bash set MSYS_NO_PATHCONV=1")
        if _SECRET.search(name):
            sys.exit(f"{name} looks like a secret; set it in the Render dashboard")
        _call("PUT", f"/services/{service}/env-vars/{name}", {"value": value})
        print("set", name)
    print("variables take effect on the next deploy")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("deploy", "set-env", "status"):
        p = sub.add_parser(name)
        p.add_argument("service", choices=sorted(SERVICES))
        if name == "deploy":
            p.add_argument("--commit")
            p.add_argument("--no-wait", action="store_true")
        if name == "set-env":
            p.add_argument("pairs", nargs="+")
    args = parser.parse_args()
    service = SERVICES[args.service]
    if args.command == "deploy":
        deploy(service, args.commit, wait=not args.no_wait)
    elif args.command == "set-env":
        set_env(service, args.pairs)
    else:
        status(service)


if __name__ == "__main__":
    main()
