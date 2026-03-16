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

- `server.py` — FastMCP server; registers 5 tools (`sru_list_servers`, `sru_explain`, `sru_list_indexes`, `sru_search`, `sru_search_books`). Thin layer that delegates to `sru.py`.
- `sru.py` — SRU protocol client. Raw HTTP via httpx, XML parsing via xmltodict. Contains CQL builder, response parsers (MARCXML + Dublin Core), and markdown formatters.
- `servers.json` — Server registry. Add new SRU endpoints here; loaded at import time.
- `test_sru.py` / `test_server.py` — Unit tests. HTTP mocked with respx. Server tool tests use `unittest.mock.patch`.

## Key Patterns

- **Namespace fallback**: SRU XML uses `zs:` prefix inconsistently. Use `_first(d, "zs:key", "key")` to handle both.
- **MARC tag dispatch**: `_MARC_TAG_HANDLERS` dict maps tag strings to handler functions. Add new tags by writing a `_marc_XXX` function and registering it.
- **`_ensure_list()`**: xmltodict returns dicts for single elements, lists for multiples. Always wrap with `_ensure_list()` before iterating.
- **Server resolution**: Tools accept either a server ID (e.g., `"loc"`) or a raw URL. `_resolve_url()` in `server.py` handles the lookup.

## Testing

- Tests use `pytest-asyncio` for async functions and `respx` for HTTP mocking
- `test_sru.py`: Pure function tests + mocked SRU operations
- `test_server.py`: Tool-level tests with `unittest.mock.patch` on `sru.explain` / `sru.search_retrieve`

## Adding a New Server

Add an entry to `servers.json` with keys: `id`, `name`, `url`, `version`, `default_schema`, `default_index`, `notes`.
