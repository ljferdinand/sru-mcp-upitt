# SRU MCP Server

MCP server for searching library catalogs via the SRU (Search/Retrieve via URL) protocol.

## Commands

```bash
python server.py                                      # Run MCP server (stdio transport)
python3 -m pytest test_sru.py test_server.py -v        # Run all tests
python3 -m py_compile sru.py server.py                 # Syntax check
npx @modelcontextprotocol/inspector python server.py   # Test with MCP Inspector
```

## Architecture

- `server.py` — FastMCP server; registers 6 tools (`sru_list_servers`, `sru_explain`, `sru_list_indexes`, `sru_search`, `sru_search_books`, `sru_scan`). Thin layer that delegates to `sru.py`.
- `sru.py` — SRU protocol client. Raw HTTP via httpx, XML parsing via xmltodict. Contains the CQL builder, response parsers (MARCXML + Dublin Core), markdown formatters, and the SRU operations (`explain`, `search_retrieve`, `scan`).
- `servers.json` — Server registry. Add new SRU endpoints here; loaded at import time.
- `test_sru.py` / `test_server.py` — Unit tests. HTTP mocked with respx. Server tool tests use `unittest.mock.patch`.

## Key Patterns

- **Per-server SRU version**: each request reads the `version` field from the server's `servers.json` record (via `_server_version()`, default 1.2). Do not hardcode a version in the operations.
- **No record-packing parameter**: `explain`/`search_retrieve` omit `recordPacking`/`recordXMLEscaping` entirely. `xml` is the SRU default, and sending the parameter makes the LoC `lx2` endpoint return HTTP 500. Do not re-add it.
- **Per-server extra params**: `_server_extra_params()` reads an optional `extra_params` object from a server's record and merges it into every request. Used for endpoints that need extra query params, e.g. the KB's `x-collection=GGC`.
- **Index sets vary per server**: `build_cql` (used by `sru_search_books`) selects a field-to-index map by `index_set` (keyword-only, default `dc`). `server.py` passes the server's `default_index`. `_INDEX_SETS` holds `dc` (`dc.*`/`bath.isbn`/`cql.anywhere`) and `alma` (`alma.*`, names verified against the Pitt endpoint). Unknown sets fall back to `dc`. A `dc.*` query against an Alma server returns zero results, not an error.
- **Namespace-agnostic parsing**: SRU XML uses the `zs:`/`srw:` prefix inconsistently across servers and versions; some bind the SRW namespace to a prefix, some make it the default namespace (no prefix at all), and the explain payload may itself be prefixed. Match elements by *localname* using `_get_ns(d, "localname", ...)`, which ignores any prefix, rather than enumerating specific prefixes. `_get_ns` is also safe on non-dicts (returns `None`). When `force_list` may have wrapped a single element (e.g. a lone `<record>`) in a list, drill in with `_unwrap()` first. `_first` is kept for plain key lookups and is now list-safe (returns `None` instead of doing a membership test against a list).
- **MARC tag dispatch**: `_MARC_TAG_HANDLERS` maps tag strings to handler functions. Add new tags by writing a `_marc_XXX` function and registering it.
- **`_ensure_list()`**: xmltodict returns dicts for single elements, lists for multiples. Always wrap with `_ensure_list()` before iterating.
- **Server resolution**: tools accept either a server ID (e.g., `"loc"`) or a raw URL. `_resolve_url()` in `server.py` handles the lookup; `get_server()` in `sru.py` returns the full record (including `version`, `default_index`, `extra_params`).

## Testing

- Tests use `pytest-asyncio` for async functions and `respx` for HTTP mocking.
- `test_sru.py`: pure function tests plus mocked SRU operations.
- `test_server.py`: tool-level tests with `unittest.mock.patch` on `sru.explain` / `sru.search_retrieve`.

## Adding a New Server

Add an entry to `servers.json` with keys: `id`, `name`, `url`, `version`, `default_schema`, `default_index`, `notes`, and optional `extra_params`. Set `version` to what the endpoint expects (1.1 or 1.2). If the server uses a non-`dc` index set, set `default_index` and add a map in `_INDEX_SETS` so field search emits the right indexes.

## Notes and Limitations

- LoC `lx2.loc.gov` (`loc`, `loc-names`, `loc-subjects`): SRU 1.1, and it returns HTTP 500 if `recordPacking` is sent, so the client omits it. Verify live after deployment.
- KB (`kb`) uses `http://jsru.kb.nl/sru/sru` with `extra_params {"x-collection": "GGC"}`. The old `jsru.kb.nl/sru` path is retired.

## Changelog — 2026-06-30 (C6.30.2026a)

Diagnosed and fixed the live "Unknown / 0 indexes" failure in `sru_explain`/`sru_list_indexes` (and hardened search/scan parsing):

1. **Namespace-agnostic + list-safe response parsing.** Root cause was twofold and produced the identical symptom on every server (pitt/loc/dnb): (a) `_first` did a membership test against a `force_list`-wrapped `[record]` and silently failed; (b) the parser looked up `databaseInfo`/`indexInfo`/etc. by bare name, missing prefixed or default-namespace payloads. Added `_localname`, `_get_ns`, `_unwrap`; made `_first` list-safe; rewrote `parse_explain`, `_parse_indexes`, `parse_search_results`, `_parse_record_data`, `_parse_dublin_core`, `_parse_marcxml`, control-field extraction, `parse_scan_results`, and all envelope unwraps to match by localname.
2. **Default relevance sort for Alma.** `build_cql` now appends `sortBy alma.rank/sort.descending` for the `alma` index set unless an explicit `sort` is given (`sort=""` disables). Without it, Alma returns title-alphabetical order, which looked like broken relevance.
3. **Per-server record schema.** `sru_search`/`sru_search_books` resolve `record_schema` from each server's `default_schema` (`server_default_schema`) instead of hardcoding `marcxml` (which 500'd/`requestedRecordSchema`-errored against DNB's `oai_dc`).
4. **Alma 50-record cap** enforced in `sru_search_books`.
5. **Sortable-index discovery.** Explain parsing records each index's `sort="true"` flag; `sru_explain` summarizes sortable indexes and `sru_list_indexes` adds a Sortable column.

Regression tests added in `test_sru.py`: `TestExplainNamespaceVariants` (prefixed + default-namespace payloads), `TestFirstIsListSafe`, `TestBuildCQLSort`, `TestSchemaResolvers`. Verified offline against a faithful xmltodict stand-in (29/29 checks). **Still pending: live re-test against pitt/loc/dnb after redeploy** to confirm against real server payloads.

## Capability discovery layer (added 2026-06-30, C6.30.2026a)

Self-configuring layer built on the now-working explain parser. Design: Option A (discovery validates/augments curated maps; never generates them) + on-disk cache.

- **Cache:** `~/.sru-mcp/explain_cache.json` (outside the source tree — runtime state shouldn't sit next to versioned code, and it survives `git clean`). Keyed by server id (or URL for ad-hoc servers). Stores the *distilled* profile (title, schemas list, indexes-with-sortability, sortable list, fetched_at), not raw XML — the hardened parsing runs at fetch time. Cache has a `version` field; a mismatched/corrupt file resets to empty rather than misreading. `_save_cache` never raises (best-effort).
- **Precedence (load-bearing):** explicit servers.json value > discovered profile > hardcoded default. servers.json ALWAYS wins — this is the LoC-version-pin lesson (explain can lie). Discovery never overrides curated config.
- **Key functions in sru.py:** `discover_capabilities(server)` (async, the only discovery network fetch; reuses explain()+parse_explain()), `get_capabilities(server)` (sync cache read, returns None if undiscovered), `index_exists()` / `index_is_sortable()` (return None when undiscovered — callers MUST treat None as "unknown, don't warn/block"), `fields_to_indexes(index_set, fields)` (maps provided friendly fields to CQL indexes for validation).
- **Validation in sru_search_books:** maps provided fields to indexes, warns (advisory `> ⚠️` block, never blocks) when a mapped index is `index_exists() is False`. Skips `cql.anywhere` (a CQL context-set index, not a catalog index). Undiscovered servers (None) produce no warnings.
- **`alma.rank` caveat baked in:** index_is_sortable is advisory only. alma.rank works as a sort key but is NOT in Alma's advertised sortable list, so sort validation must warn at most, never reject.
- **New tool:** `sru_refresh_capabilities(server)` — fetches + caches, reports index/sortable/schema counts. Not read-only (writes cache), so no readOnlyHint.

Tested offline: 16 discovery checks + permanent tests (TestCapabilityDiscovery, TestFieldsToIndexes) in test_sru.py. Full prior suite still 29/29.

NEEDS REDEPLOY + live test: `sru_refresh_capabilities(pitt)` should cache ~404 indexes; then `sru_search_books(pitt, subject="...")` with a deliberately bad mapping would warn (alma.subjects IS valid, so to see a warning you'd need a server whose curated map points at a missing index — mainly this proves the no-false-positive path on real data).
