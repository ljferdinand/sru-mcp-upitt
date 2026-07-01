# sru-mcp

An MCP server that searches library catalogs worldwide using the [SRU (Search/Retrieve via URL)](https://www.loc.gov/standards/sru/) protocol. No API keys required, since SRU is an open standard supported by national libraries, university catalogs, and consortia.

## Features

- **10 tools** for LLM-driven bibliographic search
- **9 pre-configured servers** including the Library of Congress, BnF, DNB, the KB, Trove, and a University of Pittsburgh (Ex Libris Alma) endpoint
- **Add your own library** with `sru_add_target` — no JSON editing. It builds the endpoint URL for your ILS platform, verifies it with a live probe, and registers it (see [Add your own library](#add-your-own-library) below)
- Sends the SRU **version per server** (1.1 / 1.2) from `servers.json`, and omits the optional `recordPacking` parameter (`xml` is the SRU default, and some servers such as the LoC reject it when present)
- Parses **MARCXML** and **Dublin Core** record formats
- Supports any SRU-compliant server: pass a URL or use a built-in server ID
- Raw CQL queries or high-level field-based search (title, author, ISBN, subject, etc.)
- Per-server index sets, so field search uses the right indexes (e.g. `alma.*` for Alma)

## Requirements

- Python 3.10 or newer (the source uses `str | None` annotations evaluated at import time)
- Dependencies: `mcp[cli]`, `httpx`, `xmltodict`
- Claude Desktop (or any MCP client) to use the tools

---

## Install on Windows (from scratch)

This walkthrough assumes a fresh Windows machine with nothing installed yet. You need three things: Python, the project files, and Claude Desktop.

### 1. Install Python

- Go to <https://www.python.org/downloads/windows/> and download the latest "Windows installer (64-bit)".
- Run the installer. On the first screen, **check the box "Add python.exe to PATH"**, then click "Install Now".
- Verify it: open PowerShell (press Start, type `PowerShell`, press Enter) and run:

  ```powershell
  python --version
  ```

  You should see `Python 3.10` or higher. If you get an error, close and reopen PowerShell, or reinstall and make sure the PATH box was checked.

  (Alternative, if you prefer: `winget install Python.Python.3.12` in PowerShell.)

### 2. Get the project files

- On the project's GitHub page, click the green **Code** button, then **Download ZIP**.
- Extract the ZIP, and place the folder at `C:\Users\<you>\sru-mcp` (replace `<you>` with your Windows username). You should end up with the file `C:\Users\<you>\sru-mcp\server.py`.
- No Git is required. If you do have Git, this works too: `git clone https://github.com/codefzer/sru-mcp C:\Users\<you>\sru-mcp`.

### 3. Create an isolated environment and install dependencies

- Open PowerShell in the project folder. The easy way: open `C:\Users\<you>\sru-mcp` in File Explorer, click the address bar, type `powershell`, and press Enter. Or run `cd C:\Users\<you>\sru-mcp`.
- Create and activate a virtual environment:

  ```powershell
  python -m venv .venv
  .venv\Scripts\Activate.ps1
  ```

  If PowerShell blocks the second line with an execution-policy error, run this once and then re-run the activate line:

  ```powershell
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
  ```

  Your prompt should now begin with `(.venv)`.

- Install the dependencies:

  ```powershell
  python -m pip install --upgrade pip
  pip install "mcp[cli]" httpx xmltodict
  ```

### 4. Confirm it runs

```powershell
python server.py
```

The server should start and then sit quietly, waiting for a client on standard input. That silence is the success case for this kind of server. Press `Ctrl+C` to stop it. If you see a red error or traceback instead, something in step 3 did not complete; recheck it.

### 5. Install Claude Desktop

If you do not already have it, download Claude Desktop from <https://claude.ai/download>, install it, and sign in.

### 6. Connect the server to Claude Desktop

- Open the config file from PowerShell:

  ```powershell
  notepad $env:APPDATA\Claude\claude_desktop_config.json
  ```

  If Notepad offers to create the file, click Yes.

- Paste the following, replacing `<you>` with your username. If the file already had content, add only the `sru` block inside the existing `mcpServers` object rather than overwriting everything:

  ```json
  {
    "mcpServers": {
      "sru": {
        "command": "C:\\Users\\<you>\\sru-mcp\\.venv\\Scripts\\python.exe",
        "args": ["C:\\Users\\<you>\\sru-mcp\\server.py"]
      }
    }
  }
  ```

  The doubled backslashes are required by JSON. Pointing `command` at the venv's `python.exe` means Claude Desktop uses the environment where the dependencies were installed.

- Save and close Notepad.

### 7. Restart and verify

- Fully quit Claude Desktop: right-click its icon in the system tray (near the clock), choose **Quit**. Closing the window is not enough.
- Reopen Claude Desktop. The SRU tools should now be available. A good first test is to ask it to run `sru_list_servers`.

---

## Other platforms (quick install)

```bash
pip install "mcp[cli]" httpx xmltodict   # or: pip install .
python server.py
```

Then add the server to your Claude Desktop config. On macOS the file is `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sru": {
      "command": "python",
      "args": ["/path/to/sru-mcp/server.py"]
    }
  }
}
```

Use the path to the interpreter that has the dependencies (the venv's `python` if you made one).

## Tools

| Tool | Description |
|------|-------------|
| `sru_list_servers` | List all catalog servers (built-in and user-added) |
| `sru_list_platforms` | List the ILS platforms `sru_add_target` supports and the inputs each needs |
| `sru_add_target` | Add your own library's SRU endpoint (builds the URL, probes it, registers it) |
| `sru_remove_target` | Remove a server you added with `sru_add_target` |
| `sru_explain` | Get a server's capabilities, supported schemas, and indexes |
| `sru_list_indexes` | List available search indexes on a server (use to discover index names for CQL) |
| `sru_search` | Execute a raw CQL query |
| `sru_search_books` | Field-based search (title, author, ISBN, subject, publisher, year, keyword) |
| `sru_scan` | Browse index terms near a given term (SRU scan operation) |
| `sru_refresh_capabilities` | Fetch and cache a server's explain profile (indexes, sortability, schemas) |

### Example Usage

Field-based search against the German National Library (works out of the box):

```
sru_search_books(server="dnb", keyword="Goethe")
```

Field-based search against the University of Pittsburgh catalog. Field search automatically uses Alma's `alma.*` indexes for this server:

```
sru_search_books(server="pitt", title="Moby Dick", author="Melville")
```

Raw CQL, discovering index names first:

```
sru_list_indexes(server="pitt", filter_text="title")
sru_search(server="pitt", cql_query='alma.title all "moby dick" AND alma.creator all melville')
```

## Add your own library

Most people have never met SRU, because it is historically an *institution-to-institution* protocol — the plumbing behind copy cataloging, interlibrary loan, and discovery layers, not something end users touch. But if you do work with a specific library's catalog, SRU is a real, standardized way to search it directly, and this server lets you register your library and query it like any built-in one. No `servers.json` editing required.

### What to expect

- The tool searches and retrieves **bibliographic metadata** (title, author, subject, ISBN, and so on) and returns catalog **records** in MARCXML or Dublin Core. It does not retrieve full text.
- Indexes and record schemas **vary per institution**, which is why the server discovers them (via the explain probe) rather than assuming.
- Some catalogs require **authentication** the tool does not manage, and **many institutions do not expose SRU publicly at all**. A failed add usually means "that endpoint is not open," not "the tool is broken."

### Finding your endpoint

Run `sru_list_platforms` to see what each platform needs. Then call `sru_add_target`. The inputs depend on your library's system:

- **Alma** (Ex Libris): supply your **Alma domain** and **institution code**. The domain is the host in your browser's address bar when you are logged into Alma (for example `pitt.alma.exlibrisgroup.com`, or a datacenter form like `eu03.alma.exlibrisgroup.com`). The institution code looks like `01PITT_INST` and lives in Alma's configuration, or a systems librarian can tell you. Note that **SRU is off by default in Alma** and must be enabled institution-side; if the probe fails, that is the first thing to check.
- **Koha / FOLIO**: supply the **host** (and for FOLIO the tenant **dbname**); the port and database default to the usual values (`9999`/`biblios` for Koha, `9997` for FOLIO) and can be overridden. These endpoints are frequently internal-only or an optional add-on, so a public probe may not answer. Your systems administrator has these details.
- **Other / any SRU server**: if you already have a working SRU **base URL** from anywhere, use the `other` platform and pass it directly. The record schema is chosen from what the server advertises in its explain response.

### Example

```
sru_list_platforms
sru_add_target(platform="alma", name="My University", domain="myuni.alma.exlibrisgroup.com", institution_code="01MYUNI_INST")
sru_search_books(server="my-university", keyword="climate")
sru_remove_target(key="my-university")   # if you want to undo it
```

The added server is written to `~/.sru-mcp/user_servers.json`, works immediately in the session, and persists across restarts. It shows up in `sru_list_servers` marked "user-added". Built-in servers cannot be overwritten or removed. Credentials, if you pass them, are used only for the probe and are never stored.

## Pre-configured Servers

| ID | Name | Version | Schema | Notes |
|----|------|---------|--------|-------|
| `loc` | Library of Congress | 1.1 | dc | US national catalog |
| `loc-names` | LC Name Authority | 1.1 | dc | Personal and corporate names (same host as `loc`) |
| `loc-subjects` | LC Subject Authority | 1.1 | dc | Subject headings and genre terms (same host as `loc`) |
| `bnf` | Bibliothèque nationale de France | 1.2 | dublincore | French national library |
| `dnb` | Deutsche Nationalbibliothek | 1.1 | oai_dc | German national library |
| `kb` | Koninklijke Bibliotheek | 1.1 | dc | Netherlands national library; GGC catalog via `x-collection=GGC` |
| `bibsys` | BIBSYS | 1.1 | dc | Norwegian academic libraries |
| `trove` | National Library of Australia (Trove) | 1.1 | dc | Australian national library aggregator |
| `pitt` | University of Pittsburgh | 1.2 | marcxml | ULS, Ex Libris Alma; institution code `01PITT_INST`; uses the `alma` CQL index set |

### Adding a server

Two ways. For most users, `sru_add_target` (above) is the easiest and needs no file editing. To add a server permanently in the repo (so it ships as a built-in), edit `servers.json`. Each entry has these keys:

- `id`, `name`, `url`, `notes`
- `version`: the SRU version the endpoint expects (`1.1` or `1.2`). The client sends this per request.
- `default_schema`: the record schema label for the server.
- `default_index`: the CQL index set the field-based search should use (`dc` by default, `alma` for Ex Libris Alma).
- `extra_params` (optional): an object of extra query parameters appended to every request, for endpoints that need them (for example, the KB requires `{"x-collection": "GGC"}`).

Servers you add with `sru_add_target` are stored separately in `~/.sru-mcp/user_servers.json` (not the repo). Resolution precedence is **`servers.json` > `user_servers.json` > discovered profile > built-in default**, so a shipped server always wins and a user entry can never shadow it.

## Search syntax and index sets

`sru_search_books` builds CQL from the field-to-index map for the server's `default_index`:

- `dc` (default): `dc.title`, `dc.creator`, `dc.subject`, `dc.publisher`, `dc.date`, `bath.isbn`, `cql.anywhere`.
- `alma` (Ex Libris Alma, e.g. `pitt`): `alma.title`, `alma.creator`, `alma.isbn`, `alma.subjects`, `alma.publisher`, `alma.main_pub_date`, `alma.all_for_ui`.

For servers using any other index set, or to build precise queries, use `sru_search` with raw CQL, and run `sru_list_indexes` first to discover the index names.

CQL relations: `=` is an exact or phrase match; `all` matches records containing all the given words and favors recall. For a specific title, `alma.title = "moby dick"` is tighter than `alma.title all "moby dick"`.

### Result sorting

Alma's SRU endpoint returns matches in ascending title-alphabetical order unless a `sortBy` clause is sent, which makes a correct result set look mis-ranked (e.g. unrelated titles starting with digits or "A" sorting above an exact match). For `alma` index-set servers, `sru_search_books` therefore appends `sortBy alma.rank/sort.descending` (relevance) by default. Pass an explicit `sort` (e.g. `alma.title/sort.ascending`) to override, or `sort=""` to disable sorting and use the server's own default order. Only indexes flagged sortable in the explain response can be used in a sort clause; `sru_explain` and `sru_list_indexes` now show which indexes are sortable.

## Capability discovery (self-configuring)

The server can fetch a catalog's explain document once, distill it into a
compact capability profile, and cache that profile on disk
(`~/.sru-mcp/explain_cache.json`). Run `sru_refresh_capabilities` against a
server to populate it (and `sru_add_target` populates it automatically when it
registers a new server). The profile records which indexes the server exposes,
which are sortable, and which record schemas it supports.

Discovery is layered strictly behind the hand-maintained config — precedence is
always **explicit `servers.json` value > discovered profile > hardcoded
default**. `servers.json` wins because explain documents can be wrong (the LoC
`lx2` endpoint reports version 2.0 in explain but actually serves 1.1). The
curated field-to-index maps (e.g. author → `alma.creator`) remain the source of
truth for *which* index a friendly field means — Alma exposes several
creator-like indexes, and choosing the right one is human judgment, not a
heuristic. Discovery only *validates and annotates*:

- **Validation:** `sru_search_books` checks each mapped index against the
  discovered profile and warns when an index is missing (so a misconfigured
  server surfaces a clear message instead of a silent zero-result) rather than
  blocking the query.
- **Sortability:** the discovered `sortable` list is advisory only. Some working
  sort keys are not advertised as sortable (Alma honors `alma.rank` for
  relevance even though explain omits it from the sortable list), so discovery
  never rejects a sort — it informs.

Discovery is an enhancement, never a dependency: an undiscovered or unreachable
server behaves exactly as before, using `servers.json` plus the built-in
defaults. The cache tolerates a missing or corrupt file by starting fresh.

## Record schemas

The requested schema defaults to each server's `default_schema` from `servers.json` (resolved per server), not a single hardcoded value: LoC and several others use `dc`, DNB uses `oai_dc`, and Alma uses `marcxml`. Requesting a schema the server does not support yields a `requestedRecordSchema` diagnostic, so passing `record_schema` explicitly is only needed to request a non-default schema the server also supports. Use `sru_explain` to see the supported schemas.

## Notes and limitations

- **Protocol version and record packing:** the version is sent per server from the `version` field (1.1 or 1.2). The client omits `recordPacking` entirely; `xml` is the SRU default, and the LoC `lx2` endpoints return HTTP 500 when `recordPacking` is present. SRU 1.2-only servers such as Alma return empty result sets if queried with 1.1, so keep each server's `version` accurate.
- **Response parsing is namespace-agnostic:** explain, searchRetrieve, and scan responses are parsed by element localname regardless of the namespace prefix a server uses (`zs:`, `srw:`, a prefix on the explain payload, or the SRW namespace as the default with no prefix). A lone `<record>` wrapped in a list by `force_list` is also handled. Earlier versions matched only specific prefixes, which made `sru_explain`/`sru_list_indexes` silently return "Unknown / 0 indexes" on real servers.
- **Alma record cap:** Alma rejects `maximumRecords` above 50, so `sru_search_books` caps it at 50 for `alma` servers.
- **Index sets:** field-based `sru_search_books` only matches when the server's `default_index` map fits the server. For anything unusual, use `sru_search` with the server's own indexes.
- **Koninklijke Bibliotheek:** uses `http://jsru.kb.nl/sru/sru` with `x-collection=GGC` to target the GGC catalog. The older `jsru.kb.nl/sru` path is retired.

## Development

```bash
# Install dev dependencies
pip install pytest pytest-asyncio respx

# Run tests
python3 -m pytest test_sru.py test_server.py -v
python3 test_targets.py          # identity-discovery core (pure, no network deps)

# Syntax check
python3 -m py_compile sru.py server.py targets.py

# Test with MCP Inspector
npx @modelcontextprotocol/inspector python server.py
```

## Project Structure

```
server.py          FastMCP server — registers the tools
sru.py             SRU protocol client — HTTP, CQL builder, parsing, formatting, capability cache
targets.py         Identity-discovery: platform templates, URL assembly, user_servers persistence
servers.json       Built-in server registry (loaded at import time)
test_sru.py        Tests for sru.py
test_server.py     Tests for server.py
test_targets.py    Tests for targets.py (pure, offline)
```

At runtime the server also reads and writes `~/.sru-mcp/` (outside the repo): `user_servers.json` for servers you add, and `explain_cache.json` for discovered capabilities.

## License

MIT
