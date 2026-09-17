"""Verifies the vendored Shared Model Execution RC1 freeze package against PIN.json.

Standalone (stdlib only) so it can run before the package is installed. Exit status 0 means every
pinned digest, byte count, and the normative-extract identity hold; anything else fails closed.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BEGIN_MARKER = "<!-- BEGIN NORMATIVE EXTRACT: contract RC1 §§11-13.1 -->\n".encode()
END_MARKER = "\n<!-- END NORMATIVE EXTRACT: contract RC1 §§11-13.1 -->".encode()
SOURCE_START = b"## 11. Canonical request identity and idempotency"
SOURCE_END = b"\n\n## 14. "


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_from_companion(companion: bytes) -> bytes:
    if companion.count(BEGIN_MARKER) != 1 or companion.count(END_MARKER) != 1:
        raise ValueError("companion must contain exactly one BEGIN and one END marker")
    start = companion.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = companion.index(END_MARKER)
    if end <= start:
        raise ValueError("normative extract markers are out of order")
    return companion[start:end]


def extract_from_contract(contract: bytes) -> bytes:
    if contract.count(SOURCE_START) != 1 or contract.count(SOURCE_END) != 1:
        raise ValueError("contract source sections are not uniquely delimited")
    start = contract.index(SOURCE_START)
    end = contract.index(SOURCE_END)
    return contract[start:end]


def verify(root: Path = HERE) -> list[str]:
    pin = json.loads((root / "PIN.json").read_text(encoding="utf-8"))
    artifacts = pin["artifacts"]
    problems: list[str] = []
    loaded: dict[str, bytes] = {}

    for role in ("wire_and_architecture_contract", "runtime_recovery_companion"):
        entry = artifacts[role]
        data = (root / entry["path"]).read_bytes()
        loaded[role] = data
        if b"\r\n" in data:
            problems.append(f"{role}: CRLF line endings present")
        if len(data) != entry["bytes"]:
            problems.append(f"{role}: expected {entry['bytes']} bytes, found {len(data)}")
        if _sha256(data) != entry["sha256"]:
            problems.append(f"{role}: SHA-256 mismatch")

    manifest = artifacts["freeze_manifest"]
    manifest_bytes = (root / manifest["path"]).read_bytes()
    if _sha256(manifest_bytes) != manifest["sha256"]:
        problems.append("freeze_manifest: SHA-256 mismatch")
    manifest_doc = json.loads(manifest_bytes)
    for listed in manifest_doc["artifacts"]:
        role = listed["role"]
        if role not in artifacts or listed["sha256"] != artifacts[role]["sha256"]:
            problems.append(f"freeze_manifest: {role} digest disagrees with PIN.json")

    extract_pin = artifacts["normative_recovery_extract"]
    if manifest_doc["normative_extract"]["sha256"] != extract_pin["sha256"]:
        problems.append("freeze_manifest: normative extract digest disagrees with PIN.json")
    try:
        companion_extract = extract_from_companion(loaded["runtime_recovery_companion"])
        contract_extract = extract_from_contract(loaded["wire_and_architecture_contract"])
    except ValueError as exc:
        problems.append(f"normative_recovery_extract: {exc}")
    else:
        if len(companion_extract) != extract_pin["bytes"]:
            problems.append("normative_recovery_extract: byte count mismatch")
        if _sha256(companion_extract) != extract_pin["sha256"]:
            problems.append("normative_recovery_extract: SHA-256 mismatch")
        if companion_extract != contract_extract:
            problems.append("normative_recovery_extract: companion copy differs from contract")

    return problems


def main() -> int:
    problems = verify()
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1
    print("OK shared model execution RC1 pin verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
