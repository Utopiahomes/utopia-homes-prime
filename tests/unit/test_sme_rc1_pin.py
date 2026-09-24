"""The vendored Shared Model Execution RC1 freeze package is exactly the pinned bytes, and Homes'
exact-string tables are checked against the pinned contract text rather than transcription."""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path

import pytest

from utopia_homes_prime.inference import sme_wire

PIN_DIR = Path(__file__).resolve().parents[2] / "contracts" / "stoin-shared-model-execution-v1-rc1"
CONTRACT = PIN_DIR / "docs" / "stoin-shared-model-execution-contract-v1-rc1.md"
COMPANION = PIN_DIR / "docs" / "stoin-shared-model-execution-runtime-recovery-v1-rc1.md"

OWNER_SUPPLIED_PINS = {
    "contract": "a010c2cd5d501dd5586be3e1c54753ed7bf82505b9971d19c007e227bb9a75a8",
    "companion": "9ac574affc4cd5c9dc5266b4665d64409cf387abc69c59918d7fbe0475ddbc34",
    "extract": "d8cb89be105e4507f493e5ad88d365834fc5538bb1e4f315701325e195810176",
}


def _verifier():
    spec = importlib.util.spec_from_file_location("verify_pin", PIN_DIR / "verify_pin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pin_verifies_against_owner_supplied_digests():
    import hashlib
    import json

    verifier = _verifier()
    assert verifier.verify() == []
    pin = json.loads((PIN_DIR / "PIN.json").read_text(encoding="utf-8"))["artifacts"]
    assert pin["wire_and_architecture_contract"]["sha256"] == OWNER_SUPPLIED_PINS["contract"]
    assert pin["runtime_recovery_companion"]["sha256"] == OWNER_SUPPLIED_PINS["companion"]
    assert pin["normative_recovery_extract"]["sha256"] == OWNER_SUPPLIED_PINS["extract"]
    extract = verifier.extract_from_companion(COMPANION.read_bytes())
    assert hashlib.sha256(extract).hexdigest() == OWNER_SUPPLIED_PINS["extract"]


@pytest.mark.parametrize("target", ["contract", "companion_extract", "manifest"])
def test_pin_detects_tampering(tmp_path, target):
    copy = tmp_path / "pin"
    shutil.copytree(PIN_DIR, copy)
    if target == "contract":
        path = copy / "docs" / CONTRACT.name
        path.write_bytes(path.read_bytes().replace(b"exactly one", b"exactly two", 1))
    elif target == "companion_extract":
        path = copy / "docs" / COMPANION.name
        path.write_bytes(path.read_bytes().replace(b"five seconds", b"six seconds", 1))
    else:
        path = copy / "freeze-manifest.json"
        path.write_bytes(path.read_bytes().replace(b'"rc1"', b'"rc2"', 1))
    assert _verifier().verify(copy)


def _contract_section(heading: str, next_heading: str) -> str:
    text = CONTRACT.read_text(encoding="utf-8")
    return text[text.index(heading) : text.index(next_heading)]


def test_error_table_matches_pinned_contract_exactly():
    section = _contract_section("## 16. Error contract", "## 17.")
    rows = re.findall(r"^\| (\d{3}) \| `([a-z_]+)` \| `([^`]+)` \| (yes|no) \|", section, re.M)
    pinned = {code: (int(http), message, flag == "yes") for http, code, message, flag in rows}
    ours = {
        code: (spec.http_status, spec.message, spec.retryable)
        for code, spec in sme_wire.ERROR_TABLE.items()
    }
    assert len(pinned) == 29
    assert ours == pinned


def test_retry_after_codes_match_pinned_contract():
    section = _contract_section("`Retry-After` appears only for", "## 17.")
    listed = set(re.findall(r"`([a-z_]+)`", section.split("It MUST")[0])) - {"Retry-After"}
    assert listed == set(sme_wire.RETRY_AFTER_CODES)
    assert listed == {code for code, spec in sme_wire.ERROR_TABLE.items() if spec.retryable}


def test_schema_keywords_match_pinned_contract():
    section = _contract_section("#### 9.3.2 JSON Schema", "### 9.4")
    sentence = section[section.index("Supported schema keywords are only:") :].split(".")[0]
    assert set(re.findall(r"`([A-Za-z]+)`", sentence)) == set(sme_wire.SCHEMA_KEYWORDS)
