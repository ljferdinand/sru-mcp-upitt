"""SRU (Search/Retrieve via URL) protocol client.

Implements the SRU 1.1/1.2 standard via raw HTTP GET requests.
Spec: https://www.loc.gov/standards/sru/
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import xmltodict


# ---------------------------------------------------------------------------
# Server registry  (loaded from servers.json next to this file)
# ---------------------------------------------------------------------------

_SERVERS_FILE = Path(__file__).parent / "servers.json"

# List of all server records: [{id, name, url, version, default_schema, ...}]
SERVERS: list[dict[str, str]] = json.loads(_SERVERS_FILE.read_text())

# Convenience mapping: id → url  (kept for backwards-compat and tool descriptions)
KNOWN_SERVERS: dict[str, str] = {s["id"]: s["url"] for s in SERVERS}


def get_server(id_or_url: str) -> dict[str, str] | None:
    """Return the server record for a given ID or URL, or None if not found."""
    for s in SERVERS:
        if s["id"] == id_or_url or s["url"] == id_or_url:
            return s
    return None


# ---------------------------------------------------------------------------
# CQL query builder
# ---------------------------------------------------------------------------

_FIELD_INDEX: dict[str, str] = {
    "title":     "dc.title",
    "author":    "dc.creator",
    "isbn":      "bath.isbn",
    "subject":   "dc.subject",
    "publisher": "dc.publisher",
    "year":      "dc.date",
    "keyword":   "cql.anywhere",
}


def build_cql(**fields: str | None) -> str:
    """Build a CQL query string from common bibliographic fields.

    Only fields with non-empty values are included. Multiple fields are
    AND-chained.

    Raises ValueError if no fields are provided.
    """
    clauses: list[str] = []
    for field, value in fields.items():
        if not value:
            continue
        index = _FIELD_INDEX.get(field)
        if index is None:
            raise ValueError(f"Unknown search field: {field!r}. "
                             f"Valid fields: {list(_FIELD_INDEX)}")
        # Quote the term if it contains spaces
        term = f'"{value}"' if " " in value else value
        clauses.append(f"{index} = {term}")
    if not clauses:
        raise ValueError(
            "At least one search field must be provided. "
            f"Valid fields: {list(_FIELD_INDEX)}"
        )
    return " AND ".join(clauses)


# ---------------------------------------------------------------------------
# Low-level HTTP helper
# ---------------------------------------------------------------------------

async def _get_xml(
    url: str,
    params: dict[str, str],
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Perform an HTTP GET and parse the XML response into a dict."""
    auth = (username, password) if username and password else None
    async with httpx.AsyncClient(auth=auth, timeout=30.0, follow_redirects=True) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise SRUError(
                f"Request timed out connecting to {url}. "
                "Check the server URL or your network connection."
            )
        except httpx.HTTPStatusError as exc:
            raise SRUError(
                f"HTTP {exc.response.status_code} from {url}. "
                "Verify the server URL is correct."
            )
        except httpx.RequestError as exc:
            raise SRUError(
                f"Could not reach SRU server at {url}: {exc}. "
                f"Try a known server like 'loc' ({KNOWN_SERVERS['loc']})."
            )
        content_type = response.headers.get("content-type", "")
        text = response.text

    if not text.strip():
        raise SRUError(f"Empty response from {url}.")
    try:
        return xmltodict.parse(text, force_list=("record", "index", "schema",
                                                  "set", "term"))
    except Exception as exc:
        raise SRUError(
            f"Server at {url} returned non-XML content "
            f"(content-type: {content_type}). "
            "Is this an SRU endpoint?"
        ) from exc


# ---------------------------------------------------------------------------
# SRU operations
# ---------------------------------------------------------------------------

class SRUError(Exception):
    """Raised for protocol-level or connectivity errors."""


async def explain(
    server_url: str,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Execute an SRU explain request and return the parsed response dict."""
    params = {
        "operation": "explain",
        "version": "1.1",
        "recordPacking": "xml",
    }
    data = await _get_xml(server_url, params, username, password)
    return _first(data, "zs:explainResponse", "explainResponse") or data


async def search_retrieve(
    server_url: str,
    cql_query: str,
    max_records: int = 10,
    start_record: int = 1,
    record_schema: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Execute an SRU searchRetrieve request and return the parsed response."""
    params: dict[str, str] = {
        "operation": "searchRetrieve",
        "version": "1.1",
        "query": cql_query,
        "maximumRecords": str(max_records),
        "startRecord": str(start_record),
        "recordPacking": "xml",
    }
    if record_schema:
        params["recordSchema"] = record_schema

    data = await _get_xml(server_url, params, username, password)
    root = _first(data, "zs:searchRetrieveResponse", "searchRetrieveResponse") or data

    # Surface diagnostic errors from the server
    diag = _first(root, "zs:diagnostics", "diagnostics")
    if diag:
        msg_block = _first(diag, "diag:diagnostic", "diagnostic") or diag
        if isinstance(msg_block, list):
            msg_block = msg_block[0]
        message = (
            _first(msg_block, "diag:message", "message")
            or str(msg_block)
        )
        raise SRUError(f"SRU server diagnostic: {message}")

    return root


async def scan(
    server_url: str,
    scan_clause: str,
    max_terms: int = 20,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Execute an SRU scan request to browse index terms."""
    params = {
        "operation": "scan",
        "version": "1.1",
        "scanClause": scan_clause,
        "maximumTerms": str(max_terms),
    }
    data = await _get_xml(server_url, params, username, password)
    return _first(data, "zs:scanResponse", "scanResponse") or data


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

def parse_explain(root: dict[str, Any]) -> dict[str, Any]:
    """Extract useful info from an explain response.

    Handles the SRU 1.1 envelope:
      zs:explainResponse → zs:record → zs:recordData → explain
    """
    record = _first(root, "zs:record", "record") or {}
    record_data = _first(record, "zs:recordData", "recordData") or {}
    explain_block = record_data.get("explain") or record_data

    db_info = explain_block.get("databaseInfo") or {}
    index_info = explain_block.get("indexInfo") or {}
    schema_info = explain_block.get("schemaInfo") or {}
    config_info = explain_block.get("configInfo") or {}

    title = _text(db_info, "title") or "Unknown"
    description = _text(db_info, "description") or ""

    schemas: list[dict] = []
    for s in _ensure_list(schema_info.get("schema")):
        schemas.append({
            "name": s.get("@name", ""),
            "identifier": s.get("@identifier", ""),
            "title": s.get("title") or s.get("@name", ""),
        })

    defaults: dict[str, str] = {}
    for d in _ensure_list(config_info.get("default")):
        dtype = d.get("@type", "")
        val = _text(d, "#text") or d.get("@value", "")
        if dtype and val:
            defaults[dtype] = str(val)

    return {
        "title": title,
        "description": description,
        "schemas": schemas,
        "defaults": defaults,
        "indexes": _parse_indexes(index_info),
    }


def _parse_indexes(index_info: dict) -> list[dict]:
    """Extract index list from explain indexInfo block.

    Structure (no namespace prefix):
      indexInfo.index[].map (dict or list) → name: {#text, @set}
    """
    indexes: list[dict] = []
    for idx in _ensure_list(index_info.get("index")):
        title = _text(idx, "title") or ""
        for m in _ensure_list(idx.get("map")):
            name_block = m.get("name") or {}
            if isinstance(name_block, str):
                indexes.append({"set": "", "name": name_block, "title": title})
            elif isinstance(name_block, dict):
                indexes.append({
                    "set": name_block.get("@set", ""),
                    "name": name_block.get("#text", ""),
                    "title": title,
                })
    return indexes


def parse_search_results(root: dict[str, Any]) -> dict[str, Any]:
    """Extract records from a searchRetrieve response."""
    total_raw = _first(root, "zs:numberOfRecords", "numberOfRecords") or "0"
    try:
        total = int(total_raw)
    except (ValueError, TypeError):
        total = 0

    next_raw = _first(root, "zs:nextRecordPosition", "nextRecordPosition")
    next_position = int(next_raw) if next_raw else None

    records_block = _first(root, "zs:records", "records") or {}
    raw_records = _first(records_block, "zs:record", "record") or []
    if isinstance(raw_records, dict):
        raw_records = [raw_records]

    parsed: list[dict] = []
    for rec in raw_records:
        schema = _first(rec, "zs:recordSchema", "recordSchema") or ""
        record_data = _first(rec, "zs:recordData", "recordData") or {}
        parsed.append(_parse_record_data(record_data, schema))

    return {
        "total": total,
        "next_position": next_position,
        "records": parsed,
    }


def _parse_record_data(record_data: dict | str, schema: str) -> dict:
    """Parse a single record's data into a flat dict."""
    if isinstance(record_data, str):
        return {"raw": record_data, "schema": schema}

    # Try Dublin Core (various namespace prefixes)
    dc = (
        record_data.get("srw_dc:dc")
        or record_data.get("oai_dc:dc")
        or record_data.get("dc")
    )
    if dc:
        return _parse_dublin_core(dc, schema)

    # Try MARCXML — xmltodict force_list may produce a list for "record"
    marc_raw = record_data.get("record") or record_data.get("marc:record")
    if marc_raw is not None:
        marc = marc_raw[0] if isinstance(marc_raw, list) else marc_raw
        if isinstance(marc, dict):
            return _parse_marcxml(marc, schema)

    return {"raw": record_data, "schema": schema}


def _parse_dublin_core(dc: dict, schema: str) -> dict:
    result: dict[str, Any] = {"schema": schema or "dc"}
    mapping = {
        "title":       ("dc:title",       "title"),
        "author":      ("dc:creator",     "creator"),
        "subject":     ("dc:subject",     "subject"),
        "description": ("dc:description", "description"),
        "publisher":   ("dc:publisher",   "publisher"),
        "date":        ("dc:date",        "date"),
        "type":        ("dc:type",        "type"),
        "format":      ("dc:format",      "format"),
        "identifier":  ("dc:identifier",  "identifier"),
        "language":    ("dc:language",    "language"),
    }
    for out_key, (prefixed, plain) in mapping.items():
        val = dc.get(prefixed) or dc.get(plain)
        if val:
            result[out_key] = _listify(val)
    return result


def _parse_marcxml(marc: dict, schema: str) -> dict:
    """Parse MARCXML record into common fields."""
    result: dict[str, Any] = {"schema": schema or "marcxml"}

    for field in _ensure_list(marc.get("datafield") or marc.get("marc:datafield")):
        tag = field.get("@tag", "")
        sfs = _ensure_list(field.get("subfield") or field.get("marc:subfield"))
        handler = _MARC_TAG_HANDLERS.get(tag)
        if handler:
            handler(result, sfs)

    _extract_control_fields(result, marc)
    return result


def _marc_020(result: dict, sfs: list) -> None:
    isbn = _subfield(sfs, "a")
    if isbn:
        result.setdefault("isbn", []).append(isbn.split()[0])


def _marc_100(result: dict, sfs: list) -> None:
    a = _subfield(sfs, "a")
    if a:
        result.setdefault("author", []).append(a.rstrip(", "))


def _marc_245(result: dict, sfs: list) -> None:
    parts = [p for p in [_subfield(sfs, "a"), _subfield(sfs, "b")] if p]
    if parts:
        result["title"] = " ".join(parts).rstrip(" /")


def _marc_250(result: dict, sfs: list) -> None:
    edition = _subfield(sfs, "a")
    if edition:
        result["edition"] = edition


def _marc_pub(result: dict, sfs: list) -> None:
    """Handle 260/264 (publication info). First-seen wins via setdefault."""
    pub = _subfield(sfs, "b")
    year = _subfield(sfs, "c")
    if pub:
        result.setdefault("publisher", pub.rstrip(","))
    if year:
        result.setdefault("year", year.strip("., "))


def _marc_300(result: dict, sfs: list) -> None:
    pages = _subfield(sfs, "a")
    if pages:
        result["extent"] = pages


def _marc_note(result: dict, sfs: list) -> None:
    note = _subfield(sfs, "a")
    if note:
        result.setdefault("notes", []).append(note)


def _marc_650(result: dict, sfs: list) -> None:
    subj = _subfield(sfs, "a")
    if subj:
        result.setdefault("subject", []).append(subj.rstrip(". "))


def _marc_700(result: dict, sfs: list) -> None:
    contrib = _subfield(sfs, "a")
    if contrib:
        result.setdefault("contributors", []).append(contrib.rstrip(", "))


def _marc_856(result: dict, sfs: list) -> None:
    url = _subfield(sfs, "u")
    if url:
        result.setdefault("urls", []).append(url)


_MARC_TAG_HANDLERS: dict[str, Any] = {
    "020": _marc_020,
    "100": _marc_100,
    "245": _marc_245,
    "250": _marc_250,
    "260": _marc_pub,
    "264": _marc_pub,
    "300": _marc_300,
    "500": _marc_note,
    "520": _marc_note,
    "650": _marc_650,
    "700": _marc_700,
    "856": _marc_856,
}


def _extract_control_fields(result: dict, marc: dict) -> None:
    """Extract year and language from the MARC 008 control field."""
    for cf in _ensure_list(marc.get("controlfield") or marc.get("marc:controlfield")):
        if cf.get("@tag") == "008":
            fixed = cf.get("#text", "")
            if len(fixed) >= 38:
                if "year" not in result:
                    year_str = fixed[7:11]
                    if year_str.isdigit():
                        result["year"] = year_str
                lang_code = fixed[35:38].strip()
                if lang_code:
                    result["language"] = lang_code
            break


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_explain_markdown(info: dict) -> str:
    """Render explain info as a human-readable markdown string."""
    lines = [f"## {info['title']}"]
    if info.get("description"):
        lines.append(f"\n{info['description']}")

    if info.get("schemas"):
        lines.append("\n### Supported Record Schemas")
        for s in info["schemas"]:
            name = s.get("title") or s.get("name")
            identifier = s.get("identifier", "")
            lines.append(f"- **{name}** (`{identifier}`)" if identifier else f"- **{name}**")

    if info.get("defaults"):
        lines.append("\n### Server Defaults")
        for k, v in info["defaults"].items():
            lines.append(f"- {k}: {v}")

    lines.append(f"\n### Available Indexes ({len(info.get('indexes', []))} total)")
    lines.append("Use `sru_list_indexes` to see the full index list.")

    return "\n".join(lines)


def format_indexes_markdown(indexes: list[dict], filter_text: str | None = None) -> str:
    """Render index list as a markdown table."""
    if filter_text:
        fl = filter_text.lower()
        indexes = [i for i in indexes if fl in i.get("name", "").lower()
                   or fl in i.get("title", "").lower()]

    if not indexes:
        return "No indexes found matching the filter."

    lines = ["| Context Set | Index Name | Title |",
             "|-------------|------------|-------|"]
    for idx in indexes:
        lines.append(f"| {idx.get('set','')} | {idx.get('name','')} | {idx.get('title','')} |")
    return "\n".join(lines)


def format_search_results_markdown(results: dict) -> str:
    """Render search results as markdown."""
    total = results["total"]
    records = results["records"]
    if total == 0:
        return (
            "**No records found.**\n\n"
            "Try broader search terms, check the index names with "
            "`sru_list_indexes`, or use a different server."
        )

    lines = [f"**Found {total} record(s)** — showing {len(records)}\n"]
    for i, rec in enumerate(records, 1):
        lines.append(f"### {i}. {_join(rec.get('title', ['[No title]']))}")
        if rec.get("author"):
            lines.append(f"**Author:** {_join(rec['author'])}")
        if rec.get("publisher"):
            lines.append(f"**Publisher:** {_join(rec['publisher'])}")
        if rec.get("year"):
            lines.append(f"**Year:** {rec['year']}")
        if rec.get("isbn"):
            lines.append(f"**ISBN:** {_join(rec['isbn'])}")
        if rec.get("subject"):
            lines.append(f"**Subjects:** {'; '.join(_listify(rec['subject'])[:5])}")
        if rec.get("language"):
            lines.append(f"**Language:** {rec['language']}")
        if rec.get("extent"):
            lines.append(f"**Extent:** {rec['extent']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _first(d: dict, *keys: str) -> Any:
    """Return the value of the first key present in d."""
    for k in keys:
        if k in d:
            return d[k]
    return None


def _text(d: dict, *keys: str) -> str | None:
    """Return the first matching key's value, unwrapping {'#text': ...} dicts."""
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return v.get("#text") or v.get("@value")
        if isinstance(v, list) and v:
            return str(v[0])
    return None


def _ensure_list(val: Any) -> list:
    """Return val as a list; wraps non-list values, returns [] for None."""
    if isinstance(val, list):
        return val
    if val is None:
        return []
    return [val]


def _listify(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(v) for v in val]
    return [str(val)]


def _join(val: Any) -> str:
    if isinstance(val, list):
        return "; ".join(str(v) for v in val)
    return str(val)


def _subfield(subfields: list, code: str) -> str | None:
    """Return the text of the first subfield with the given code."""
    for sf in subfields:
        if sf.get("@code") == code:
            return sf.get("#text", "")
    return None
