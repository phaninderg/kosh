"""Every statement in samples/ must parse and match its manifest entry; every PDF there must have an entry.
This is the contract for contributors: a new layout comes with a dummy PDF that passes here."""

import json
from pathlib import Path

import pytest

from samples.describe import REGISTRY, summarize

ROOT = Path(__file__).resolve().parent.parent / "samples"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())


@pytest.mark.parametrize("entry", MANIFEST, ids=[e["file"] for e in MANIFEST])
def test_sample_parses_as_the_manifest_says(entry):
    st = REGISTRY[entry["parser"]].parse(ROOT / entry["file"], entry["password"])
    assert summarize(st) == entry["expect"]
    for a in getattr(st, "accounts", []):
        assert a.integrity_errors() == []


def test_every_pdf_in_samples_is_listed_in_the_manifest():
    listed = {e["file"] for e in MANIFEST}
    assert {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.pdf")} == listed


def test_samples_cover_every_registered_statement_parser():
    assert {e["parser"] for e in MANIFEST} >= {"icici", "icici_card", "hdfc_card", "axis_card", "tata_card", "nps"}
