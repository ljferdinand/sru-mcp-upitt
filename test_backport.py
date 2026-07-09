"""Regression tests for the 0.3.2 backport (Node line -> Python fork).

These verify the fixes ported from the Node repo's 0.3.x line. Kept in a
separate module so the backport can be reviewed as a unit and, if ever
upstreamed to codefzer, cherry-picked cleanly. Framework: pytest, like the rest
of the suite. Sections are added per backport step (B, C, D, E).
"""

import pytest

import sru
import targets


# --- Step B / #2: bnf 'bib' CQL context set ---------------------------------

def test_bib_index_set_registered():
    assert "bib" in sru._INDEX_SETS
    assert sru._INDEX_SETS["bib"]["keyword"] == "bib.anywhere"


def test_build_cql_bib_field_mapping():
    # A multi-field bib query maps each field to its bib.* index and AND-chains.
    q = sru.build_cql(index_set="bib", title="les misérables", author="hugo")
    assert 'bib.title = "les misérables"' in q
    assert "bib.author = hugo" in q
    assert " AND " in q
    # bib declares no default relevance sort, so no sortBy clause is appended.
    assert "sortBy" not in q


def test_build_cql_bib_keyword_uses_bib_anywhere():
    # The bug being fixed: bib fell back to dc and emitted cql.anywhere, which
    # BnF rejects. With the bib map, keyword must resolve to bib.anywhere.
    q = sru.build_cql(index_set="bib", keyword="hugo")
    assert q == "bib.anywhere = hugo"
    assert "cql.anywhere" not in q


def test_fields_to_indexes_bib():
    planned = sru.fields_to_indexes("bib", {"title": "x", "author": "y", "isbn": None})
    assert planned == {"title": "bib.title", "author": "bib.author"}


# --- Step C / #3: unwrapped Dublin Core record parse (KB jsru) --------------

def test_has_dublin_core_elements():
    # Unwrapped DC: dc:* elements sit directly under recordData.
    assert sru._has_dublin_core_elements({"dc:title": "x", "dc:creator": "y"}) is True
    # MARCXML recordData's child is <record>, whose localname is not a DC
    # element, so this must not mis-fire on MARC.
    assert sru._has_dublin_core_elements({"record": {"@xmlns": "..."}}) is False
    # A wrapped-DC recordData carries a <dc> child (localname "dc"), which is
    # handled by the earlier branch and is deliberately NOT a DC element name.
    assert sru._has_dublin_core_elements({"srw_dc:dc": {"dc:title": "x"}}) is False
    assert sru._has_dublin_core_elements("not a dict") is False


def test_parse_unwrapped_dublin_core():
    # KB's jsru endpoint returns dc:* elements directly under recordData with no
    # <dc> wrapper (unlike LoC/DNB, which wrap them in srw_dc:dc). The parser
    # must extract fields rather than fall through to {"raw": ...} ("[No title]").
    record_data = {
        "dc:title": "Rembrandt 'Rembrandt'",
        "dc:creator": "Giltaij, Jeroen",
        "dc:subject": ["Rembrandt", "schilderkunst"],
        "dc:language": "dut",
    }
    parsed = sru._parse_record_data(record_data, "dc")
    assert parsed["title"] == ["Rembrandt 'Rembrandt'"]
    assert parsed["author"] == ["Giltaij, Jeroen"]
    assert parsed["subject"] == ["Rembrandt", "schilderkunst"]
    assert "raw" not in parsed


# --- Step D / #6: scan diagnostic surfacing ---------------------------------

def test_diagnostic_message_scan_response_shape():
    # LoC lx2 returns the unsupported-scan diagnostic inside a scanResponse,
    # namespaced with a zs: prefix. _diagnostic_message reads it namespace-
    # agnostically (scan() resolves the scanResponse envelope before calling).
    root = {
        "zs:diagnostics": {
            "zs:diagnostic": {
                "zs:uri": "info:srw/diagnostic/1/4",
                "zs:details": "scan",
                "zs:message": "Unsupported operation",
            }
        }
    }
    assert sru._diagnostic_message(root) == "Unsupported operation"


def test_diagnostic_message_alma_envelope_shape():
    # Alma reports the unsupported scan as a diagnostic wrapped in a
    # searchRetrieveResponse envelope; scan() resolves that envelope, then
    # _diagnostic_message extracts the message identically.
    root = {
        "diagnostics": {
            "diagnostic": {
                "uri": "info:srw/diagnostic/1/4",
                "message": "The sru operation is not supported",
            }
        }
    }
    assert sru._diagnostic_message(root) == "The sru operation is not supported"


def test_diagnostic_message_details_fallback_and_none():
    # Falls back to details when there is no message element.
    root = {"diagnostics": {"diagnostic": {"details": "boom"}}}
    assert sru._diagnostic_message(root) == "boom"
    # A normal response with no diagnostics yields None (no error raised).
    assert sru._diagnostic_message({"records": {}}) is None


# --- Step E / #1: add_target schema-retrieval validation --------------------

def test_is_schema_diagnostic():
    assert targets.is_schema_diagnostic("Unknown record schema for retrieval") is True
    assert targets.is_schema_diagnostic("Unsupported record schema") is True
    # A non-schema diagnostic (e.g. unsupported scan) must not match, so a real
    # failure is not mistaken for a wrong-schema signal.
    assert targets.is_schema_diagnostic("The sru operation is not supported") is False
    assert targets.is_schema_diagnostic(None) is False
    assert targets.is_schema_diagnostic("") is False


def test_schema_candidates_order_and_dedup():
    # Guessed first, then advertised, then common fallbacks; duplicates collapse.
    assert targets.schema_candidates("other", ["mods", "dc"], "marcxml") == \
        ["marcxml", "mods", "dc", "oai_dc"]
    # No advertised schemas: guessed, then the fallback set.
    assert targets.schema_candidates("alma", [], "marcxml") == \
        ["marcxml", "dc", "oai_dc"]
    # Falsy entries dropped.
    assert targets.schema_candidates("other", [None, ""], "") == \
        ["marcxml", "dc", "oai_dc"]


@pytest.mark.asyncio
async def test_select_validated_schema_confirms_first_working():
    # The first candidate returns records on its first probe -> confirmed.
    async def run(schema, query):
        return {"records": 3 if schema == "marcxml" else 0, "schema_error": False}
    result = await targets.select_validated_schema(["marcxml", "dc"], run)
    assert result == ("marcxml", "confirmed")


@pytest.mark.asyncio
async def test_select_validated_schema_skips_schema_error_then_confirms():
    # marcxml raises a schema diagnostic (skip it); dc then confirms.
    async def run(schema, query):
        if schema == "marcxml":
            return {"records": 0, "schema_error": True}
        return {"records": 5, "schema_error": False}
    result = await targets.select_validated_schema(["marcxml", "dc"], run)
    assert result == ("dc", "confirmed")


@pytest.mark.asyncio
async def test_select_validated_schema_assumes_first_when_inconclusive():
    # Nothing returns records and nothing is a schema error -> fall back to the
    # guessed default (first candidate), marked "assumed" so the caller caveats.
    async def run(schema, query):
        return {"records": 0, "schema_error": False}
    result = await targets.select_validated_schema(["marcxml", "dc"], run)
    assert result == ("marcxml", "assumed")
