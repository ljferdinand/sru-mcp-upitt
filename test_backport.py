"""Regression tests for the 0.3.2 backport (Node line -> Python fork).

These verify the fixes ported from the Node repo's 0.3.x line. Kept in a
separate module so the backport can be reviewed as a unit and, if ever
upstreamed to codefzer, cherry-picked cleanly. Framework: pytest, like the rest
of the suite. Sections are added per backport step (B, C, D, E).
"""

import sru


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
