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

import targets


# ---------------------------------------------------------------------------
# Server registry  (loaded from servers.json next to this file)
# ---------------------------------------------------------------------------

_SERVERS_FILE = Path(__file__).parent / "servers.json"

# List of all server records: [{id, name, url, version, default_schema, ...}]
SERVERS: list[dict[str, str]] = json.loads(_SERVERS_FILE.read_text())

# Convenience mapping: id → url  (kept for backwards-compat and tool descriptions)
KNOWN_SERVERS: dict[str, str] = {s["id"]: s["url"] for s in SERVERS}


def get_server(id_or_url: str) -> dict[str, Any] | None:
    """Return the server record for a given ID or URL, or None if not found.

    Checks shipped servers.json first, then user-added servers from
    ~/.sru-mcp/user_servers.json (registered via sru_add_target). servers.json
    wins on an id/url collision, so a user entry can never shadow a curated one.
    User servers are read fresh from disk on each call, so a target added during
    a session is immediately usable without a restart."""
    for s in SERVERS:
        if s["id"] == id_or_url or s["url"] == id_or_url:
            return s
    for s in targets.load_user_servers():
        if s.get("id") == id_or_url or s.get("url") == id_or_url:
            return s
    return None


def all_servers() -> list[dict[str, Any]]:
    """Shipped servers plus user-added ones, shipped winning on id collision.

    Used by sru_list_servers (display) and by sru_add_target (uniqueness check
    for new keys). A user entry whose id duplicates a shipped one is dropped
    from the merged view, matching get_server's precedence."""
    shipped_ids = {s["id"] for s in SERVERS}
    merged: list[dict[str, Any]] = list(SERVERS)
    for u in targets.load_user_servers():
        if u.get("id") not in shipped_ids:
            merged.append(u)
    return merged


def _server_version(server_url: str, default: str = "1.2") -> str:
    """Return the SRU protocol version declared for a server (servers.json),
    defaulting to 1.2. searchRetrieve/scan previously hardcoded 1.1, which made
    SRU 1.2-only servers (e.g. Ex Libris Alma) return zero results."""
    s = get_server(server_url)
    return (s or {}).get("version") or default


def _server_extra_params(server_url: str) -> dict[str, str]:
    """Optional per-server query params declared in servers.json under the
    "extra_params" object (e.g. the KB catalog's x-collection=GGC). Merged into
    every request to that server."""
    s = get_server(server_url)
    extra = (s or {}).get("extra_params") or {}
    return {str(k): str(v) for k, v in extra.items()}


def server_default_schema(id_or_url: str, fallback: str = "marcxml") -> str:
    """Return the record schema a server expects by default (servers.json
    "default_schema"). Requesting a schema the server doesn't support yields a
    'requestedRecordSchema' diagnostic (e.g. DNB rejects marcxml; it wants
    oai_dc), so field-based search should resolve this per server instead of
    hardcoding one schema for all."""
    s = get_server(id_or_url)
    return (s or {}).get("default_schema") or fallback


def server_default_index(id_or_url: str, fallback: str = "dc") -> str:
    """Return the CQL index set for a server (servers.json "default_index"),
    e.g. "alma" for Ex Libris Alma endpoints, "dc" otherwise."""
    s = get_server(id_or_url)
    return (s or {}).get("default_index") or fallback


# ---------------------------------------------------------------------------
# Capability discovery + on-disk cache
# ---------------------------------------------------------------------------
#
# A server's explain document declares its real capabilities: which indexes
# exist, which are sortable, and which record schemas are supported. We fetch
# and distill that ONCE into a compact per-server "capability profile", cache
# it on disk, and use it to *validate and annotate* curated config — never to
# override it. Precedence is always: explicit servers.json value > discovered
# profile > hardcoded default. servers.json wins because explain can be wrong
# (e.g. LoC's lx2 reports version 2.0 in explain but serves 1.1).
#
# The cache lives outside the source tree (runtime state shouldn't sit next to
# versioned code and should survive a `git clean`).

_CACHE_DIR = Path.home() / ".sru-mcp"
_CACHE_FILE = _CACHE_DIR / "explain_cache.json"

# Cache schema version, so a future format change can invalidate old entries.
_CACHE_VERSION = 1


def _cache_key(id_or_url: str) -> str:
    """Stable cache key: the server id if known, else the raw URL."""
    s = get_server(id_or_url)
    return s["id"] if s else id_or_url


def _load_cache() -> dict[str, Any]:
    """Read the on-disk cache, tolerant of a missing or corrupt file."""
    try:
        data = json.loads(_CACHE_FILE.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {"version": _CACHE_VERSION, "servers": {}}
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
        # Unknown/old format: start fresh rather than misread it.
        return {"version": _CACHE_VERSION, "servers": {}}
    data.setdefault("servers", {})
    return data


def _save_cache(cache: dict[str, Any]) -> bool:
    """Write the cache to disk, creating the directory. Returns success;
    never raises (caching is best-effort and must not break a search)."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))
        return True
    except OSError:
        return False


def _profile_from_explain(info: dict[str, Any]) -> dict[str, Any]:
    """Distill parse_explain() output into a compact capability profile.

    Stores the parsed/distilled form (not raw XML) so the parsing we hardened
    runs at fetch time, not on every cache read."""
    indexes: dict[str, dict[str, Any]] = {}
    sortable: list[str] = []
    for idx in info.get("indexes", []):
        iset = idx.get("set", "")
        name = idx.get("name", "")
        if not name:
            continue
        qualified = f"{iset}.{name}" if iset else name
        is_sortable = bool(idx.get("sort"))
        indexes[qualified] = {"sortable": is_sortable, "title": idx.get("title", "")}
        if is_sortable:
            sortable.append(qualified)
    return {
        "title": info.get("title", ""),
        "schemas": [s.get("name", "") for s in info.get("schemas", []) if s.get("name")],
        "indexes": indexes,
        "sortable": sortable,
    }


async def discover_capabilities(
    id_or_url: str,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the server's explain document, distill a capability profile, and
    write it to the on-disk cache. Returns the profile, or None if the server
    is unreachable / explain fails (caller degrades to servers.json + defaults).

    This is the only function that performs a network fetch for discovery."""
    try:
        root = await explain(_resolve_for_discovery(id_or_url), username, password)
        info = parse_explain(root)
    except SRUError:
        return None
    profile = _profile_from_explain(info)
    profile["fetched_at"] = _utc_now_iso()
    cache = _load_cache()
    cache["servers"][_cache_key(id_or_url)] = profile
    _save_cache(cache)
    return profile


def cache_capabilities_from_explain(id_or_url: str, info: dict[str, Any]) -> dict[str, Any]:
    """Write a capability profile to the cache from an ALREADY-parsed explain
    (parse_explain output), keyed by the server id/url. Returns the profile.

    Used by sru_add_target, which has just fetched explain to probe a new
    endpoint and should not fetch it again. Mirrors discover_capabilities'
    caching without the network round trip. Call AFTER the server is registered
    so _cache_key resolves to the server's id."""
    profile = _profile_from_explain(info)
    profile["fetched_at"] = _utc_now_iso()
    cache = _load_cache()
    cache["servers"][_cache_key(id_or_url)] = profile
    _save_cache(cache)
    return profile


def uncache_server(key: str) -> bool:
    """Drop a server's cached capability profile by id. Returns whether an entry
    was removed. Used by sru_remove_target so a removed server leaves no stale
    cache behind. Pops by the literal id (which is how add-time caching keyed
    it, since the server was registered at that point)."""
    cache = _load_cache()
    if key in cache.get("servers", {}):
        del cache["servers"][key]
        _save_cache(cache)
        return True
    return False


def get_capabilities(id_or_url: str) -> dict[str, Any] | None:
    """Return the cached capability profile for a server, or None if not yet
    discovered. Synchronous and cheap (reads the local cache). To populate or
    refresh, call discover_capabilities() (async, fetches).

    Callers MUST treat None as "no discovery data; use servers.json + defaults"
    and never fail on its absence — discovery is an enhancement, not a
    dependency."""
    cache = _load_cache()
    return cache["servers"].get(_cache_key(id_or_url))


def index_exists(id_or_url: str, index: str) -> bool | None:
    """Has the server been discovered to expose this CQL index?

    Returns True/False if a profile exists, or None if the server has not been
    discovered yet (in which case the caller cannot conclude anything and
    should proceed without a warning)."""
    profile = get_capabilities(id_or_url)
    if profile is None:
        return None
    return index in profile.get("indexes", {})


def index_is_sortable(id_or_url: str, index: str) -> bool | None:
    """Is this index advertised as sortable in the discovered profile?

    Returns True/False if discovered, None if not discovered. IMPORTANT: this
    is ADVISORY only. Some working sort keys are not advertised as sortable
    (e.g. Alma honors `alma.rank` for relevance sorting even though it is not in
    the explain sortable list), so callers must warn at most, never block."""
    profile = get_capabilities(id_or_url)
    if profile is None:
        return None
    idx = profile.get("indexes", {}).get(index)
    if idx is None:
        return None
    return bool(idx.get("sortable"))


def _resolve_for_discovery(id_or_url: str) -> str:
    """Resolve an id to its URL for the explain fetch (sru.py has no import of
    server.py's _resolve_url, so resolve locally)."""
    s = get_server(id_or_url)
    return s["url"] if s else id_or_url


def _utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string with a trailing Z."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# CQL query builder
# ---------------------------------------------------------------------------

# Field -> CQL index maps, keyed by index set. "dc" is the default and works on
# servers exposing the Dublin Core context set. Servers using a different index
# set declare it via "default_index" in servers.json; add a map here for it.
_FIELD_INDEX: dict[str, str] = {
    "title":     "dc.title",
    "author":    "dc.creator",
    "isbn":      "bath.isbn",
    "subject":   "dc.subject",
    "publisher": "dc.publisher",
    "year":      "dc.date",
    "keyword":   "cql.anywhere",
}

# Ex Libris Alma uses the "alma" index set. Index names verified against the
# University of Pittsburgh endpoint via sru_list_indexes on 2026-06-25.
_ALMA_FIELD_INDEX: dict[str, str] = {
    "title":     "alma.title",
    "author":    "alma.creator",
    "isbn":      "alma.isbn",
    "subject":   "alma.subjects",
    "publisher": "alma.publisher",
    "year":      "alma.main_pub_date",
    "keyword":   "alma.all_for_ui",
}

# Bibliothèque nationale de France uses the "bib" CQL context set. bnf's
# default_index is "bib" in servers.json, but without a map here build_cql fell
# back to dc and emitted cql.anywhere, which BnF rejects ("Index non supporté").
# Index names verified live via sru_list_indexes on the BnF endpoint 2026-07-09.
# No default relevance sort key for bib (see _DEFAULT_SORT).
_BIB_FIELD_INDEX: dict[str, str] = {
    "title":     "bib.title",
    "author":    "bib.author",
    "isbn":      "bib.isbn",
    "subject":   "bib.subject",
    "publisher": "bib.publisher",
    "year":      "bib.date",
    "keyword":   "bib.anywhere",
}

_INDEX_SETS: dict[str, dict[str, str]] = {
    "dc": _FIELD_INDEX,
    "alma": _ALMA_FIELD_INDEX,
    "bib": _BIB_FIELD_INDEX,
}


def fields_to_indexes(index_set: str, fields: dict[str, str | None]) -> dict[str, str]:
    """Map the *provided* (non-empty) friendly fields to the CQL indexes they
    will use under the given index set. Used to validate a planned query against
    a server's discovered capabilities. Unknown index sets fall back to dc."""
    field_index = _INDEX_SETS.get(index_set, _FIELD_INDEX)
    out: dict[str, str] = {}
    for field, value in fields.items():
        if value:
            idx = field_index.get(field)
            if idx:
                out[field] = idx
    return out

# Default relevance sort clause per index set. Without an explicit sortBy, Alma
# returns matches in ascending title-alphabetical order rather than by relevance,
# which makes correct result sets look mis-ranked (poetry anthologies above exact
# title matches). alma.rank is Alma's relevance sort key. Servers/index sets with
# no known relevance key are omitted here and left unsorted (server default).
_DEFAULT_SORT: dict[str, str] = {
    "alma": "alma.rank/sort.descending",
}


def build_cql(*, index_set: str = "dc", sort: str | None = None,
              **fields: str | None) -> str:
    """Build a CQL query string from common bibliographic fields.

    index_set selects the field-to-index mapping (e.g. "dc" or "alma").
    Unknown sets fall back to the Dublin Core ("dc") mapping. Only fields
    with non-empty values are included; multiple fields are AND-chained.

    sort: an explicit CQL sort spec (e.g. "alma.title/sort.ascending") to
    append as "sortBy <sort>". If None and the index set declares a default
    relevance sort (see _DEFAULT_SORT), that default is appended automatically.
    Pass sort="" to force no sort clause at all.

    Raises ValueError if no fields are provided.
    """
    field_index = _INDEX_SETS.get(index_set, _FIELD_INDEX)
    clauses: list[str] = []
    for field, value in fields.items():
        if not value:
            continue
        index = field_index.get(field)
        if index is None:
            raise ValueError(f"Unknown search field: {field!r}. "
                             f"Valid fields: {list(field_index)}")
        # Quote the term if it contains spaces
        term = f'"{value}"' if " " in value else value
        clauses.append(f"{index} = {term}")
    if not clauses:
        raise ValueError(
            "At least one search field must be provided. "
            f"Valid fields: {list(field_index)}"
        )
    query = " AND ".join(clauses)

    # Resolve sort: explicit arg wins; "" disables; None uses the set default.
    if sort is None:
        sort = _DEFAULT_SORT.get(index_set)
    if sort:
        query = f"{query} sortBy {sort}"
    return query


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


def _diagnostic_message(root: Any) -> str | None:
    """Return an SRU diagnostic message from a response root, or None if there
    are no diagnostics. Namespace-agnostic. Factored out so searchRetrieve and
    scan surface server diagnostics identically: scan previously swallowed them,
    so a server that does not support scan showed "No terms found" instead of
    the real reason (e.g. "Unsupported operation")."""
    diag = _get_ns(root, "diagnostics")
    if not diag:
        return None
    msg_block = _get_ns(diag, "diagnostic") or diag
    if isinstance(msg_block, list):
        msg_block = msg_block[0] if msg_block else {}
    return (
        _ns_text(msg_block, "message")
        or _ns_text(msg_block, "details")
        or (msg_block if isinstance(msg_block, str) else str(msg_block))
    )


async def explain(
    server_url: str,
    username: str | None = None,
    password: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Execute an SRU explain request and return the parsed response dict.

    version: force a specific SRU version for this request. If omitted, the
    version is resolved from servers.json (default 1.2). sru_add_target passes
    the platform's version explicitly, because a newly-probed endpoint is not
    yet in the registry and would otherwise default to 1.2 (wrong for the 1.1
    Koha/FOLIO endpoints)."""
    # Note: recordPacking/recordXMLEscaping is intentionally omitted. "xml" is
    # the SRU default, and sending it explicitly makes some servers (the Library
    # of Congress lx2 endpoint) return HTTP 500.
    params = {
        "operation": "explain",
        "version": version or _server_version(server_url),
    }
    params.update(_server_extra_params(server_url))
    data = await _get_xml(server_url, params, username, password)
    return _unwrap(_get_ns(data, "explainResponse")) or data


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
    # recordPacking omitted on purpose (see explain): "xml" is the SRU default
    # and sending it 500s the LoC lx2 endpoint.
    params: dict[str, str] = {
        "operation": "searchRetrieve",
        "version": _server_version(server_url),
        "query": cql_query,
        "maximumRecords": str(max_records),
        "startRecord": str(start_record),
    }
    if record_schema:
        params["recordSchema"] = record_schema
    params.update(_server_extra_params(server_url))

    data = await _get_xml(server_url, params, username, password)
    root = _unwrap(_get_ns(data, "searchRetrieveResponse")) or data

    # Surface diagnostic errors from the server (namespace-agnostic).
    message = _diagnostic_message(root)
    if message:
        raise SRUError(f"SRU server diagnostic: {message}")

    return root


async def scan(
    server_url: str,
    scan_clause: str,
    max_terms: int = 20,
    response_position: int = 1,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Execute an SRU scan request to browse index terms."""
    params = {
        "operation": "scan",
        "version": _server_version(server_url),
        "scanClause": scan_clause,
        "maximumTerms": str(max_terms),
        "responsePosition": str(response_position),
    }
    params.update(_server_extra_params(server_url))
    data = await _get_xml(server_url, params, username, password)
    # A server that does not support scan may return the diagnostic in a
    # scanResponse (LoC lx2) OR wrapped in a searchRetrieveResponse envelope
    # (Alma reports "The sru operation is not supported" that way). Resolve
    # either, then surface any diagnostic rather than returning an empty term
    # list, which showed "No terms found" and hid the real reason.
    root = (
        _unwrap(_get_ns(data, "scanResponse"))
        or _unwrap(_get_ns(data, "searchRetrieveResponse"))
        or data
    )
    message = _diagnostic_message(root)
    if message:
        raise SRUError(f"SRU server diagnostic: {message}")
    return root


def parse_scan_results(root: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract term list from a scan response (namespace-agnostic).

    Returns a list of dicts with 'term' and optional 'count' keys.
    """
    terms_node = _unwrap(_get_ns(root, "terms")) or {}
    term_list = _ensure_list(_get_ns(terms_node, "term"))
    results = []
    for t in term_list:
        if not isinstance(t, dict):
            # A bare string term with no count.
            results.append({"term": str(t)})
            continue
        value = _ns_text(t, "value") or ""
        count_raw = _ns_text(t, "numberOfRecords")
        entry: dict[str, Any] = {"term": value}
        if count_raw is not None:
            try:
                entry["count"] = int(count_raw)
            except ValueError:
                entry["count"] = count_raw
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

def parse_explain(root: dict[str, Any]) -> dict[str, Any]:
    """Extract useful info from an explain response.

    Handles the SRU envelope namespace-agnostically:
      explainResponse -> record -> recordData -> explain -> {databaseInfo, ...}

    Real servers vary in how they namespace these elements (zs:/srw: prefix,
    default namespace, or a prefix on the explain payload itself), and a lone
    <record> may be wrapped in a list by force_list. Both are handled here via
    localname matching (_get_ns) and list unwrapping (_unwrap)."""
    record = _unwrap(_get_ns(root, "record"))
    record_data = _unwrap(_get_ns(record, "recordData")) if record is not None else None
    explain_block = _get_ns(record_data, "explain") if record_data is not None else None
    # Some servers omit the <explain> wrapper and put the payload directly in
    # recordData; fall back to recordData itself in that case.
    if explain_block is None:
        explain_block = record_data
    explain_block = _unwrap(explain_block) or {}

    db_info = _unwrap(_get_ns(explain_block, "databaseInfo")) or {}
    index_info = _unwrap(_get_ns(explain_block, "indexInfo")) or {}
    schema_info = _unwrap(_get_ns(explain_block, "schemaInfo")) or {}
    config_info = _unwrap(_get_ns(explain_block, "configInfo")) or {}

    title = _text(db_info, "title") or _ns_text(db_info, "title") or "Unknown"
    description = _text(db_info, "description") or _ns_text(db_info, "description") or ""

    schemas: list[dict] = []
    for s in _ensure_list(_get_ns(schema_info, "schema")):
        if not isinstance(s, dict):
            continue
        schemas.append({
            "name": s.get("@name", ""),
            "identifier": s.get("@identifier", ""),
            "title": _ns_text(s, "title") or s.get("@name", ""),
            "sort": str(s.get("@sort", "")).lower() == "true",
        })

    defaults: dict[str, str] = {}
    for d in _ensure_list(_get_ns(config_info, "default")):
        if not isinstance(d, dict):
            continue
        dtype = d.get("@type", "")
        val = d.get("#text") or d.get("@value", "")
        if dtype and val:
            defaults[dtype] = str(val)

    return {
        "title": title,
        "description": description,
        "schemas": schemas,
        "defaults": defaults,
        "indexes": _parse_indexes(index_info),
    }


def _ns_text(d: Any, localname: str) -> str | None:
    """Namespace-agnostic version of _text: find the first child matching
    localname (ignoring prefix) and unwrap its text content."""
    val = _get_ns(d, localname)
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("#text") or val.get("@value")
    if isinstance(val, list) and val:
        first = val[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("#text") or first.get("@value")
    return None


def _parse_indexes(index_info: dict) -> list[dict]:
    """Extract index list from explain indexInfo block, namespace-agnostically.

    Structure (prefixes vary by server):
      indexInfo.index[].map (dict or list) -> name: {#text, @set} | str
    Also records whether each index is sortable (@sort="true"), which Alma uses
    to flag indexes usable in a sortBy clause.
    """
    indexes: list[dict] = []
    for idx in _ensure_list(_get_ns(index_info, "index")):
        if not isinstance(idx, dict):
            continue
        title = _ns_text(idx, "title") or ""
        sortable = str(idx.get("@sort", "")).lower() == "true"
        for m in _ensure_list(_get_ns(idx, "map")):
            if not isinstance(m, dict):
                continue
            name_block = _get_ns(m, "name") or {}
            if isinstance(name_block, list):
                name_block = name_block[0] if name_block else {}
            if isinstance(name_block, str):
                indexes.append({"set": "", "name": name_block,
                                "title": title, "sort": sortable})
            elif isinstance(name_block, dict):
                indexes.append({
                    "set": name_block.get("@set", ""),
                    "name": name_block.get("#text", ""),
                    "title": title,
                    "sort": sortable,
                })
    return indexes


def parse_search_results(root: dict[str, Any]) -> dict[str, Any]:
    """Extract records from a searchRetrieve response (namespace-agnostic)."""
    total_raw = _get_ns(root, "numberOfRecords") or "0"
    try:
        total = int(total_raw)
    except (ValueError, TypeError):
        total = 0

    next_raw = _get_ns(root, "nextRecordPosition")
    try:
        next_position = int(next_raw) if next_raw else None
    except (ValueError, TypeError):
        next_position = None

    records_block = _unwrap(_get_ns(root, "records")) or {}
    raw_records = _get_ns(records_block, "record") or []
    if isinstance(raw_records, dict):
        raw_records = [raw_records]

    parsed: list[dict] = []
    for rec in raw_records:
        if not isinstance(rec, dict):
            continue
        schema = _get_ns(rec, "recordSchema") or ""
        record_data = _unwrap(_get_ns(rec, "recordData")) or {}
        parsed.append(_parse_record_data(record_data, schema))

    return {
        "total": total,
        "next_position": next_position,
        "records": parsed,
    }


# Dublin Core element localnames, used to detect an unwrapped DC record whose
# dc:* elements sit directly under recordData with no <dc> wrapper (KB's jsru
# endpoint), unlike LoC/DNB which wrap them in <srw_dc:dc>.
_DC_ELEMENT_NAMES = frozenset({
    "title", "creator", "subject", "description", "publisher",
    "contributor", "date", "type", "format", "identifier",
    "source", "language", "relation", "coverage", "rights",
})


def _has_dublin_core_elements(node: Any) -> bool:
    """True if node is a dict with any direct child whose localname is a Dublin
    Core element. Used to catch records that carry dc:* elements directly under
    recordData with no wrapping <dc> element (KB's unwrapped form)."""
    if not isinstance(node, dict):
        return False
    for k in node:
        if _localname(k) in _DC_ELEMENT_NAMES:
            return True
    return False


def _parse_record_data(record_data: dict | str, schema: str) -> dict:
    """Parse a single record's data into a flat dict (namespace-agnostic)."""
    if isinstance(record_data, str):
        return {"raw": record_data, "schema": schema}

    # Try Dublin Core: the wrapper element's localname is "dc" regardless of the
    # namespace prefix the server uses (srw_dc:dc, oai_dc:dc, dc, etc.).
    dc = _get_ns(record_data, "dc")
    if isinstance(dc, list):
        dc = dc[0] if dc else None
    if isinstance(dc, dict):
        return _parse_dublin_core(dc, schema)

    # Try MARCXML — the localname is "record"; force_list may wrap it in a list.
    marc_raw = _get_ns(record_data, "record")
    if marc_raw is not None:
        marc = marc_raw[0] if isinstance(marc_raw, list) else marc_raw
        if isinstance(marc, dict):
            return _parse_marcxml(marc, schema)

    # Unwrapped Dublin Core: some servers (KB's jsru endpoint) put the dc:*
    # elements directly under recordData with no <dc> wrapper. Parse recordData
    # itself as the DC block when its children look like DC elements. Checked
    # after MARCXML, whose <record> children are never DC localnames, so this
    # cannot mis-fire on a MARC record.
    if _has_dublin_core_elements(record_data):
        return _parse_dublin_core(record_data, schema)

    return {"raw": record_data, "schema": schema}


def _parse_dublin_core(dc: dict, schema: str) -> dict:
    result: dict[str, Any] = {"schema": schema or "dc"}
    # Match DC elements by localname (title, creator, ...) regardless of the
    # namespace prefix the server attaches (dc:title, title, etc.).
    for out_key, localname in (
        ("title", "title"),
        ("author", "creator"),
        ("subject", "subject"),
        ("description", "description"),
        ("publisher", "publisher"),
        ("date", "date"),
        ("type", "type"),
        ("format", "format"),
        ("identifier", "identifier"),
        ("language", "language"),
    ):
        vals = _text_values(_get_ns(dc, localname))
        if vals:
            result[out_key] = vals
    return result


def _text_values(val: Any) -> list[str]:
    """Flatten a DC field value to a list of plain text strings.

    A value may be a bare string, a dict like {'@xmlns': ..., '#text': 'X'}
    (an element carrying attributes such as an inline namespace declaration, as
    LoC emits), or a list of either. Dicts are unwrapped to their #text/@value;
    entries with no text are dropped."""
    out: list[str] = []
    for item in val if isinstance(val, list) else [val]:
        if item is None:
            continue
        if isinstance(item, str):
            if item:
                out.append(item)
        elif isinstance(item, dict):
            text = item.get("#text") or item.get("@value")
            if text:
                out.append(str(text))
        else:
            out.append(str(item))
    return out


def _parse_marcxml(marc: dict, schema: str) -> dict:
    """Parse MARCXML record into common fields (namespace-agnostic)."""
    result: dict[str, Any] = {"schema": schema or "marcxml"}

    for field in _ensure_list(_get_ns(marc, "datafield")):
        if not isinstance(field, dict):
            continue
        tag = field.get("@tag", "")
        sfs = _ensure_list(_get_ns(field, "subfield"))
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
    for cf in _ensure_list(_get_ns(marc, "controlfield")):
        if not isinstance(cf, dict):
            continue
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

    idxs = info.get("indexes", [])
    sortable = [i for i in idxs if i.get("sort")]
    lines.append(f"\n### Available Indexes ({len(idxs)} total"
                 + (f", {len(sortable)} sortable" if sortable else "") + ")")
    if sortable:
        names = ", ".join(f"`{i['set']}.{i['name']}`" if i.get('set')
                          else f"`{i['name']}`" for i in sortable[:12])
        lines.append(f"Sortable (usable in a sortBy clause): {names}"
                     + (" …" if len(sortable) > 12 else ""))
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

    lines = ["| Context Set | Index Name | Title | Sortable |",
             "|-------------|------------|-------|----------|"]
    for idx in indexes:
        sortable = "✓" if idx.get("sort") else ""
        lines.append(f"| {idx.get('set','')} | {idx.get('name','')} "
                     f"| {idx.get('title','')} | {sortable} |")
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
            lines.append(f"**Year:** {_join(rec['year'])}")
        if rec.get("isbn"):
            lines.append(f"**ISBN:** {_join(rec['isbn'])}")
        if rec.get("subject"):
            lines.append(f"**Subjects:** {'; '.join(_listify(rec['subject'])[:5])}")
        if rec.get("language"):
            lines.append(f"**Language:** {_join(rec['language'])}")
        if rec.get("extent"):
            lines.append(f"**Extent:** {_join(rec['extent'])}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _first(d: dict, *keys: str) -> Any:
    """Return the value of the first key present in d.

    Safe when d is not a dict (e.g. a list produced by force_list): returns
    None rather than performing a membership test against the wrong type.
    """
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d:
            return d[k]
    return None


def _localname(key: str) -> str:
    """Strip any XML namespace prefix from a dict key.

    xmltodict (with the default process_namespaces=False) keeps the literal
    prefix in the key, e.g. 'zs:record', 'srw:recordData', or 'ns1:databaseInfo'.
    Real SRU servers vary: some bind the SRW/ZeeRex namespaces to a prefix, some
    make them the default namespace (no prefix at all). Localname comparison lets
    the parser treat all of these uniformly.
    """
    return key.split(":", 1)[1] if ":" in key else key


def _get_ns(d: Any, *localnames: str) -> Any:
    """Namespace-agnostic key lookup: return the value of the first child whose
    localname matches any of localnames, ignoring any namespace prefix. Safe on
    non-dicts (returns None)."""
    if not isinstance(d, dict):
        return None
    wanted = set(localnames)
    for k, v in d.items():
        if _localname(k) in wanted:
            return v
    return None


def _unwrap(val: Any) -> Any:
    """If val is a single-element list (e.g. a lone <record> that force_list
    wrapped), return that element; otherwise return val unchanged. Lets the
    envelope drill-down work whether or not force_list wrapped a node."""
    if isinstance(val, list) and len(val) == 1:
        return val[0]
    return val


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
