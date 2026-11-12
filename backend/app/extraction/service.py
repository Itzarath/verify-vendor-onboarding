"""Document readers used by the pipeline.

- LiveReader: the real model (OpenAI-compatible endpoint, gemini-3.8-flash by default).
- FixtureReader: TEST ONLY. Returns independently authored gold labels for the synthetic
  fixture files so the pipeline can be tested deterministically. It is never used by the
  running app unless APP_ENV=test, and every reading it produces is labelled "test-fixture".
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from ..config import BACKEND_ROOT, get_settings
from ..pdf.inspect import page_texts
from .contract import ModelField, ModelReading, validate_reading
from .provider import chain_from_settings
from .reader import ReadOutcome, config_key, read_document

FIXTURE_DIR = BACKEND_ROOT / "fixtures" / "out"


class LiveReader:
    def __init__(self):
        self.chain = chain_from_settings(get_settings())

    @property
    def configured(self) -> bool:
        return self.chain.configured

    @property
    def config_key(self) -> str:
        return config_key(self.chain)

    def describe(self) -> str:
        return self.chain.describe()

    def read(self, pdf: bytes, page_kinds: list[str]) -> ReadOutcome:
        return read_document(self.chain, pdf, page_kinds)


class FixtureReader:
    provider = "test-fixture"

    def __init__(self, fixture_dir: Path = FIXTURE_DIR):
        gold = json.loads((fixture_dir / "gold_labels.json").read_text(encoding="utf-8"))
        self._by_hash: dict[str, dict] = {}
        for name, entry in gold.items():
            p = fixture_dir / "documents" / name
            if p.exists():
                self._by_hash[hashlib.sha256(p.read_bytes()).hexdigest()] = entry
        self.fail_hashes: set[str] = set()  # tests can simulate provider outages per document

    configured = True
    config_key = "test-fixture-v5"  # bump when validation rules change

    def describe(self) -> str:
        return "test-fixture (gold labels; tests only)"

    def read(self, pdf: bytes, page_kinds: list[str]) -> ReadOutcome:
        sha = hashlib.sha256(pdf).hexdigest()
        if sha in self.fail_hashes:
            return ReadOutcome(None, "failed", None, None, None, None, [],
                               {"kind": "unavailable", "message": "simulated provider outage (test)"})
        entry = self._by_hash.get(sha)
        if entry is None:
            return ReadOutcome(None, "failed", None, None, None, None, [],
                               {"kind": "not_configured", "message": "test reader has no gold label for this file"})
        fields = []
        for key, value in entry["fields"].items():
            if key in entry.get("unreadable", []):
                fields.append(ModelField(key=key, status="unreadable", page=entry["page"]))
            elif value is None:
                fields.append(ModelField(key=key, status="not_found"))
            else:
                fields.append(ModelField(key=key, status="found", value=value, page=entry["page"], quote=value))
        reading = validate_reading(ModelReading(document_type=entry["type"], document_type_reason="gold label", fields=fields),
                                   page_kinds, page_texts(pdf))
        critical_missing = [f.key for f in reading.fields.values() if f.critical and f.status != "found"]
        return ReadOutcome(reading, "partial" if critical_missing else "complete", self.provider, "gold-labels", 5, {}, [], None)


_override = None


def set_reader_override(reader) -> None:
    """Tests install a FixtureReader here."""
    global _override
    _override = reader


@lru_cache
def _live() -> LiveReader:
    return LiveReader()


@lru_cache
def _fixture() -> FixtureReader:
    return FixtureReader()


def get_reader():
    if _override:
        return _override
    s = get_settings()
    if s.extraction_mode == "fixture":
        if s.app_env == "prod":
            raise RuntimeError("The test fixture reader is not allowed in production")
        return _fixture()
    return _live()
