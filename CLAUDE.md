# SRU MCP Server

MCP server for searching library catalogs via the SRU (Search/Retrieve via URL) protocol.

## Commands

```bash
python server.py                                              # Run MCP server (stdio transport)
python3 -m pytest test_sru.py test_server.py test_backport.py -v   # Run all tests
python3 test_targets.py                                       # Identity-discovery core tests (pure, offline)
python3 -m py_compile sru.py server.py targets.py             # Syntax check
npx @modelcontextprotocol/inspector python server.py          # Test with MCP Inspector
```

## Architecture

- `server.py` — FastMCP server; registers 10 tools (`sru_list_servers`, `sru_list_platforms`, `sru_add_target`, `sru_remove_target`, `sru_explain`, `sru_list_indexes`, `sru_search`, `sru_search_books`, `sru_scan`, `sru_refresh_capabilities`). Thin layer that delegates to `sru.py` and `targets.py`.
- `sru.py` — SRU protocol client. Raw HTTP via httpx, XML parsing via xmltodict. Contains the CQL builder, response parsers (MARCXML + Dublin Core, incl. unwrapped DC via `_has_dublin_core_elements`), markdown formatters, the SRU operations (`explain`, `search_retrieve`, `scan`), the shared diagnostic extractor (`_diagnostic_message`, used by both `search_retrieve` and `scan`), retrieval-schema validation (`validate_schema_for_retrieval`), and the on-disk capability cache.
- `targets.py` — Identity-discovery layer (pure; no httpx/xmltodict). The platform-template registry (`PLATFORM_TEMPLATES`), URL assembly (`assemble_url`), key handling (`slugify`, `resolve_key`), record-schema selection (`choose_default_schema`), the retrieval-schema validation core (`SCHEMA_PROBE_QUERIES`, `is_schema_diagnostic`, `schema_candidates`, `select_validated_schema` — I/O injected as a `run` callable), entry construction (`build_entry`), and `user_servers.json` persistence (`load_user_servers`, `register_user_server`, `remove_user_server`).
- `servers.json` — Built-in server registry. Add permanent SRU endpoints here; loaded at import time. Currently the seven-server shelf: `loc`, `loc-names`, `loc-subjects`, `bnf`, `dnb`, `kb`, `pitt`.
- `_version.py` — Single source of the package version (`__version__`), read by hatchling's dynamic version in `pyproject.toml` and importable at runtime.
- `~/.sru-mcp/user_servers.json` — Runtime store for servers added via `sru_add_target` (outside the repo).
- `test_sru.py` / `test_server.py` / `test_targets.py` / `test_backport.py` — Unit tests. `test_targets.py` is pure and needs no HTTP mocking; `test_backport.py` holds the 0.3.2 backport regression tests.

## Key Patterns

- **Per-server SRU version**: each request reads the `version` field from the server's `servers.json` record (via `_server_version()`, default 1.2). Do not hardcode a version in the operations. `explain()` and `search_retrieve()` also take an optional `version` override so `sru_add_target` can probe/validate a not-yet-registered endpoint at the platform's version (Koha/FOLIO are 1.1; the default 1.2 would be wrong for them).
- **No record-packing parameter**: `explain`/`search_retrieve` omit `recordPacking`/`recordXMLEscaping` entirely. `xml` is the SRU default, and sending the parameter makes the LoC `lx2` endpoint return HTTP 500. Do not re-add it.
- **Per-server extra params**: `_server_extra_params()` reads an optional `extra_params` object from a server's record and merges it into every request. Used for endpoints that need extra query params, e.g. the KB's `x-collection=GGC`.
- **Server resolution + precedence**: `get_server()` checks `servers.json` (SERVERS) first, then `user_servers.json` (via `targets.load_user_servers()`), so `servers.json` always wins on an id/url collision — a user entry can never shadow a shipped one. `all_servers()` returns the merged view (shipped + user, shipped winning). User servers are read fresh from disk each call, so a target added mid-session is usable immediately without a restart. Full precedence: `servers.json` > `user_servers.json` > discovered profile > hardcoded default.
- **Index sets vary per server**: `build_cql` (used by `sru_search_books`) selects a field-to-index map by `index_set` (keyword-only, default `dc`). `server.py` passes the server's `default_index`. `_INDEX_SETS` holds `dc` (`dc.*`/`bath.isbn`/`cql.anywhere`), `alma` (`alma.*`, names verified against the Pitt endpoint), and `bib` (`bib.*`, the BnF context set; keyword maps to `bib.anywhere` because BnF rejects `cql.anywhere`). Unknown sets fall back to `dc`. A `dc.*` query against an Alma or BnF server returns zero results, not an error.
- **Namespace-agnostic parsing**: SRU XML uses the `zs:`/`srw:` prefix inconsistently across servers and versions; some bind the SRW namespace to a prefix, some make it the default namespace (no prefix at all), and the explain payload may itself be prefixed. Match elements by *localname* using `_get_ns(d, "localname", ...)`, which ignores any prefix, rather than enumerating specific prefixes. `_get_ns` is also safe on non-dicts (returns `None`). When `force_list` may have wrapped a single element (e.g. a lone `<record>`) in a list, drill in with `_unwrap()` first. `_first` is kept for plain key lookups and is now list-safe (returns `None` instead of doing a membership test against a list).
- **Diagnostic surfacing (search + scan)**: `_diagnostic_message(root)` pulls an SRU `<diagnostics>` message out of a response root, namespace-agnostically, returning `None` when there are none. Both `search_retrieve` and `scan` call it and raise `SRUError` on a hit. `scan` first resolves the root as a `scanResponse` OR a `searchRetrieveResponse` envelope, because Alma returns its unsupported-scan diagnostic wrapped in the latter while LoC uses `scanResponse`. Without this, an unsupported scan looked like an empty term list ("No terms found").
- **Unwrapped Dublin Core**: `_parse_record_data` tries a wrapped `<dc>` element first, then MARCXML (`<record>`), then falls back to `_has_dublin_core_elements(record_data)` — true when the record's direct children carry DC localnames (title/creator/...). The KB returns `dc:*` elements directly under `recordData` with no `<dc>` wrapper, and this branch catches that. The unwrapped check is deliberately AFTER MARCXML so a `<record>`-shaped MARC payload cannot mis-fire it.
- **Retrieval-schema validation on add**: `sru_add_target` does not trust the schema advertised in explain. It builds `targets.schema_candidates(platform, advertised, guessed)` and calls `sru.validate_schema_for_retrieval(url, candidates, version, ...)`, which runs a one-record test search per (schema, query) and returns `(schema, "confirmed"|"assumed")`. The decision core `targets.select_validated_schema(candidates, run, queries)` is pure and takes an injected async `run`, so it unit-tests without httpx. A probe failure never blocks the add; the report caveat is shown only for the `other` platform, since the templated platforms carry a reliable default.
- **MARC tag dispatch**: `_MARC_TAG_HANDLERS` maps tag strings to handler functions. Add new tags by writing a `_marc_XXX` function and registering it.
- **`_ensure_list()`**: xmltodict returns dicts for single elements, lists for multiples. Always wrap with `_ensure_list()` before iterating.

## Testing

- Tests use `pytest-asyncio` for async functions and `respx` for HTTP mocking.
- `test_sru.py`: pure function tests plus mocked SRU operations.
- `test_server.py`: tool-level tests with `unittest.mock.patch` on `sru.explain` / `sru.search_retrieve`.
- `test_targets.py`: pure tests for the identity-discovery layer (slugify, key resolution, URL assembly per platform kind, schema selection, entry construction, and `user_servers.json` load/save/register/remove incl. corrupt-file tolerance). No network, no protocol deps.
- `test_backport.py`: regression tests for the 0.3.2 backport (#1 schema validation, #2 bnf bib set, #3 unwrapped DC, #6 scan diagnostics). The `select_validated_schema` tests inject a fake async `run`, so they need no network; they carry explicit `@pytest.mark.asyncio` markers.

## Adding a New Server

Two paths. **User-facing:** `sru_add_target(platform, name, ...)` builds the URL for the platform, probes it with explain, confirms a working record schema with a test search, and on success registers it to `~/.sru-mcp/user_servers.json` (no repo edit; `sru_remove_target(key)` undoes it). **Permanent/built-in:** add an entry to `servers.json` with keys `id`, `name`, `url`, `version`, `default_schema`, `default_index`, `notes`, and optional `extra_params`. Set `version` to what the endpoint expects (1.1 or 1.2). If the server uses a non-`dc` index set, set `default_index` and add a map in `_INDEX_SETS` (see `alma`, `bib`) so field search emits the right indexes.

## Notes and Limitations

- LoC `lx2.loc.gov` (`loc`, `loc-names`, `loc-subjects`): SRU 1.1, and it returns HTTP 500 if `recordPacking` is sent, so the client omits it. Verify live after deployment.
- KB (`kb`) uses `http://jsru.kb.nl/sru/sru` with `extra_params {"x-collection": "GGC"}`. The old `jsru.kb.nl/sru` path is retired. Its records are unwrapped Dublin Core (`dc:*` directly under `recordData`), handled by `_has_dublin_core_elements`.
- Scan is not implemented by the LoC `lx2` endpoints or Ex Libris Alma; `sru_scan` surfaces their diagnostic (via `_diagnostic_message`) rather than returning an empty list.

## Identity-discovery layer (added 2026-07-01, C7.1.2026a)

User-facing feature: register your own library's SRU endpoint and query it like a built-in server, without editing `servers.json`. Two discovery axes — *identity* discovery (which server is my library, this layer) feeds *capability* discovery (what can it do, the existing explain layer).

- **Platform-template registry** (`targets.PLATFORM_TEMPLATES`) with three `kind`s:
  - `parametric` — fixed URL from named parts. **alma**: `https://{domain}/view/sru/{institution_code}`; inputs `domain` + `institution_code`; defaults version 1.2, index `alma`, schema `marcxml`.
  - `host_based` — `{scheme}://{host}:{port}/{path}` with platform defaults. **koha**: port 9999, path/database `biblios`. **folio**: port 9997, path is the tenant `dbname`. Both version 1.1, index `dc`.
  - `direct` — no template. **other**: user supplies a full `base_url`; schema is chosen from what explain advertises (prefer marcxml, else first advertised, else oai_dc) and then confirmed with a test search.
  Adding a platform is a new row here, not a code change. Verify a pattern against a live endpoint before adding it — an unverified template is an anchor-miss waiting to happen.
- **Load-bearing Alma finding:** the Alma domain is NOT derivable from the institution code (vanity form `pitt.alma...` vs datacenter form `eu03.alma...`), so the domain is a required user input, not a guess. SRU is off by default in Alma; the explain probe is the real test of reachability.
- **Flow** (`sru_add_target`): assemble URL → resolve unique key → probe via `explain()` at the platform version → confirm a record schema with `validate_schema_for_retrieval` → on success register to `user_servers.json` + cache capabilities from the same explain (no second fetch, `sru.cache_capabilities_from_explain`) → summary; on failure a legible error and nothing written. Credentials are probe-only, never stored.
- **Key handling** (`resolve_key`): optional explicit `key`, else a slug of `name`; uniqueness enforced against `all_servers()`. An explicit collision errors; a derived collision auto-suffixes (`-2`, `-3`). A shipped id is never overwritten.
- **Removal** (`sru_remove_target`): removes only from `user_servers.json` (`targets.remove_user_server`) and drops the cached profile (`sru.uncache_server`). Built-in servers are refused.
- **Persistence:** `~/.sru-mcp/user_servers.json`, separate from the repo, tolerant of missing/corrupt file. Precedence `servers.json` > `user_servers.json` > discovered > hardcoded.
- **Tools added:** `sru_list_platforms` (readOnly), `sru_add_target`, `sru_remove_target`. `sru_list_servers` gained a Source column (built-in vs user-added).

Verified offline: `test_targets.py` 63/63 (pure logic incl. remove); plus a wiring check (ast syntax on all three modules, import behind stub httpx/xmltodict/mcp/pydantic, and `get_server`/`all_servers` precedence). **Live-verified 2026-07-01** on Monolith-Pro: `sru_list_platforms`; Alma parametric add of a Pitt user-copy (404 indexes, relevance-sorted keyword search matching built-in `pitt`); generic direct add of K10plus (210 indexes); clean failure on a nonexistent host (nothing saved); collision guard refusing a shipped key; persistence and the user-added Source marker in `sru_list_servers`.

## Changelog — 2026-06-30 (C6.30.2026a)

Diagnosed and fixed the live "Unknown / 0 indexes" failure in `sru_explain`/`sru_list_indexes` (and hardened search/scan parsing):

1. **Namespace-agnostic + list-safe response parsing.** Root cause was twofold and produced the identical symptom on every server (pitt/loc/dnb): (a) `_first` did a membership test against a `force_list`-wrapped `[record]` and silently failed; (b) the parser looked up `databaseInfo`/`indexInfo`/etc. by bare name, missing prefixed or default-namespace payloads. Added `_localname`, `_get_ns`, `_unwrap`; made `_first` list-safe; rewrote `parse_explain`, `_parse_indexes`, `parse_search_results`, `_parse_record_data`, `_parse_dublin_core`, `_parse_marcxml`, control-field extraction, `parse_scan_results`, and all envelope unwraps to match by localname.
2. **Default relevance sort for Alma.** `build_cql` now appends `sortBy alma.rank/sort.descending` for the `alma` index set unless an explicit `sort` is given (`sort=""` disables). Without it, Alma returns title-alphabetical order, which looked like broken relevance.
3. **Per-server record schema.** `sru_search`/`sru_search_books` resolve `record_schema` from each server's `default_schema` (`server_default_schema`) instead of hardcoding `marcxml` (which 500'd/`requestedRecordSchema`-errored against DNB's `oai_dc`).
4. **Alma 50-record cap** enforced in `sru_search_books`.
5. **Sortable-index discovery.** Explain parsing records each index's `sort="true"` flag; `sru_explain` summarizes sortable indexes and `sru_list_indexes` adds a Sortable column.

Regression tests added in `test_sru.py`: `TestExplainNamespaceVariants` (prefixed + default-namespace payloads), `TestFirstIsListSafe`, `TestBuildCQLSort`, `TestSchemaResolvers`. Verified offline against a faithful xmltodict stand-in (29/29 checks), then live-verified against pitt/loc/dnb after redeploy.

## Capability discovery layer (added 2026-06-30, C6.30.2026a)

Self-configuring layer built on the now-working explain parser. Design: Option A (discovery validates/augments curated maps; never generates them) + on-disk cache.

- **Cache:** `~/.sru-mcp/explain_cache.json` (outside the source tree — runtime state shouldn't sit next to versioned code, and it survives `git clean`). Keyed by server id (or URL for ad-hoc servers). Stores the *distilled* profile (title, schemas list, indexes-with-sortability, sortable list, fetched_at), not raw XML — the hardened parsing runs at fetch time. Cache has a `version` field; a mismatched/corrupt file resets to empty rather than misreading. `_save_cache` never raises (best-effort).
- **Precedence (load-bearing):** explicit servers.json value > discovered profile > hardcoded default. servers.json ALWAYS wins — this is the LoC-version-pin lesson (explain can lie). Discovery never overrides curated config.
- **Key functions in sru.py:** `discover_capabilities(server)` (async, the only discovery network fetch; reuses explain()+parse_explain()), `get_capabilities(server)` (sync cache read, returns None if undiscovered), `index_exists()` / `index_is_sortable()` (return None when undiscovered — callers MUST treat None as "unknown, don't warn/block"), `fields_to_indexes(index_set, fields)` (maps provided friendly fields to CQL indexes for validation). `cache_capabilities_from_explain()` writes a profile from an already-fetched explain (used by `sru_add_target`); `uncache_server()` drops one (used by `sru_remove_target`).
- **Validation in sru_search_books:** maps provided fields to indexes, warns (advisory `> ⚠️` block, never blocks) when a mapped index is `index_exists() is False`. Skips `cql.anywhere`. Undiscovered servers (None) produce no warnings.
- **`alma.rank` caveat baked in:** index_is_sortable is advisory only. alma.rank works as a sort key but is NOT in Alma's advertised sortable list, so sort validation must warn at most, never reject.
- **Tool:** `sru_refresh_capabilities(server)` — fetches + caches, reports index/sortable/schema counts. Not read-only (writes cache).

Tested offline (16 discovery checks + permanent tests), then live-verified after redeploy.

## Changelog — 2026-07-09 (C7.9.2026a): 0.3.2 backport from the Node line

Backported the fixes from the Node port (`ljferdinand/sru-mcp`, 0.3.2) into this Python fork, bringing the two to feature parity. Four fixes plus a server-shelf prune and a version single-source:

1. **#2 BnF `bib` context set.** Added `_BIB_FIELD_INDEX` and registered `bib` in `_INDEX_SETS`. BnF's `default_index` is `bib`; without the map, field search fell back to `dc` and emitted `cql.anywhere`, which BnF rejects ("Index non supporté"). Keyword now maps to `bib.anywhere`, and no default relevance sort is applied for `bib`.
2. **#3 Unwrapped Dublin Core (KB).** `_has_dublin_core_elements` plus a fallback branch in `_parse_record_data` parse records whose `dc:*` elements sit directly under `recordData` with no `<dc>` wrapper (KB's jsru shape), so they show real titles instead of "[No title]". Checked after MARCXML so a MARC `<record>` cannot mis-fire it.
3. **#6 Scan diagnostics.** Factored `_diagnostic_message`, shared by `search_retrieve` and `scan`; `scan` resolves a `scanResponse` OR a `searchRetrieveResponse` envelope (Alma wraps the unsupported-scan diagnostic in the latter, LoC uses `scanResponse`). An unsupported scan now surfaces the reason rather than an empty term list.
4. **#1 add_target schema-retrieval validation.** `targets` gained `SCHEMA_PROBE_QUERIES`, `is_schema_diagnostic`, `schema_candidates`, and the pure async `select_validated_schema`; `sru` gained `validate_schema_for_retrieval` and an optional `version` param on `search_retrieve`; `sru_add_target` confirms a working schema with a test search before saving and reports "(confirmed by a test search)". A failed probe never blocks the add; the caveat shows only for the `other` platform.

Also: `servers.json` pruned to the seven-server shelf (dropped `bibsys`, whose endpoint is dead after the BIBSYS-to-Alma migration, and `trove`, whose v3 SRU needs an API key). Version single-sourced to `0.3.2` via `_version.py` + a hatchling dynamic version in `pyproject.toml`. Backport regression tests live in `test_backport.py` (Steps B/C/D/E). Correct-by-inspection against the Node commits; run `pytest` to confirm. The FastMCP runtime version kwarg was intentionally not wired (uncertain whether this `mcp[cli]` release's `FastMCP` accepts `version`, and a wrong guess could break startup); the package version is single-sourced regardless. Keep config commits separate from logic-fix commits in case the logic fixes are cherry-picked upstream to codefzer.
