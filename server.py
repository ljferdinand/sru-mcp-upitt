"""SRU MCP Server

Provides tools for searching library catalogs via the SRU
(Search/Retrieve via URL) protocol.

Servers are configured in servers.json. Use sru_list_servers to see all
available servers, or pass any SRU server URL directly. Add your own library's
endpoint with sru_add_target (see sru_list_platforms for what to supply).

Run:
  python server.py
"""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

import sru
import targets

# Build a compact server list string for tool descriptions
_SERVER_HINT = ", ".join(
    f"{s['id']} ({s['name']})" for s in sru.SERVERS
)
_SERVER_URL_FIELD = Field(
    description=(
        "URL or ID of the SRU server. "
        f"Known IDs: {', '.join(sru.KNOWN_SERVERS)}. "
        "Use sru_list_servers to see full details, or pass any SRU server URL."
    )
)

mcp = FastMCP(
    "sru_mcp",
    instructions=(
        "Search library catalogs using the SRU (Search/Retrieve via URL) protocol. "
        "Use sru_list_servers to discover available servers, sru_explain to inspect "
        "a server's capabilities, sru_search_books for simple field-based searches, "
        "or sru_search for raw CQL queries. To search your own library, register it "
        "with sru_add_target (sru_list_platforms shows the inputs each ILS needs). "
        f"Available servers: {_SERVER_HINT}."
    ),
)


def _resolve_url(id_or_url: str) -> str:
    """Resolve a server ID to its URL, or return the input unchanged if it's already a URL."""
    server = sru.get_server(id_or_url)
    return server["url"] if server else id_or_url


# ---------------------------------------------------------------------------
# Tool: sru_list_servers
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"readOnlyHint": True})
def sru_list_servers() -> str:
    """List all known SRU library catalog servers.

    Returns a table with ID, name, URL, default schema, source (built-in vs
    user-added), and notes for each server. Pass the ID or URL to other sru_*
    tools. User-added servers come from sru_add_target.
    """
    shipped_ids = {s["id"] for s in sru.SERVERS}
    lines = ["| ID | Name | URL | Schema | Source | Notes |",
             "|----|------|-----|--------|--------|-------|"]
    for s in sru.all_servers():
        source = "built-in" if s["id"] in shipped_ids else "user-added"
        lines.append(
            f"| {s['id']} | {s['name']} | {s['url']} "
            f"| {s.get('default_schema', '')} | {source} | {s.get('notes', '')} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: sru_list_platforms
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"readOnlyHint": True})
def sru_list_platforms() -> str:
    """List the ILS platforms sru_add_target can register, and the inputs each needs.

    Run this before sru_add_target so you know what to supply. SRU is
    historically an institution-to-institution protocol, so if you are not sure
    what system your library runs, ask a systems librarian — or, if you already
    have a working SRU base URL, use the 'other' platform and pass it directly.
    """
    lines = ["| Platform | Kind | Required inputs | Optional (default) | Notes |",
             "|----------|------|-----------------|--------------------|-------|"]
    for p in targets.list_platforms():
        req = ", ".join(p["required_inputs"]) or "—"
        opt = ", ".join(f"{k}={v}" for k, v in p["optional_inputs"].items()) or "—"
        lines.append(f"| {p['platform']} | {p['kind']} | {req} | {opt} | {p['description']} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: sru_add_target
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"openWorldHint": True})
async def sru_add_target(
    platform: Annotated[
        str,
        Field(description="ILS platform: 'alma', 'koha', 'folio', or 'other'. See sru_list_platforms."),
    ],
    name: Annotated[
        str,
        Field(description="Human-readable name for the catalog, e.g. 'University of Pittsburgh'."),
    ],
    key: Annotated[
        str | None,
        Field(description="Optional short id used to refer to this server in other tools "
                          "(e.g. 'pitt'). Defaults to a slug of name. Must be unique; a shipped "
                          "server's id cannot be overwritten."),
    ] = None,
    domain: Annotated[
        str | None,
        Field(description="Alma: the Alma host, e.g. pitt.alma.exlibrisgroup.com — the domain in "
                          "your browser address bar when logged into Alma."),
    ] = None,
    institution_code: Annotated[
        str | None,
        Field(description="Alma: institution code, e.g. 01PITT_INST."),
    ] = None,
    host: Annotated[
        str | None,
        Field(description="Koha/FOLIO: the server hostname."),
    ] = None,
    port: Annotated[
        str | None,
        Field(description="Koha/FOLIO: the SRU port (Koha default 9999, FOLIO default 9997)."),
    ] = None,
    database: Annotated[
        str | None,
        Field(description="Koha: the SRU database (default 'biblios')."),
    ] = None,
    dbname: Annotated[
        str | None,
        Field(description="FOLIO: the tenant / database name."),
    ] = None,
    scheme: Annotated[
        str | None,
        Field(description="Koha/FOLIO: 'http' or 'https' (default http)."),
    ] = None,
    base_url: Annotated[
        str | None,
        Field(description="'other' platform: the full SRU base URL, without a query string."),
    ] = None,
    username: Annotated[
        str | None,
        Field(description="Optional HTTP basic auth username, used ONLY to probe; never stored."),
    ] = None,
    password: Annotated[
        str | None,
        Field(description="Optional HTTP basic auth password, used ONLY to probe; never stored."),
    ] = None,
) -> str:
    """Register the SRU endpoint for a library so you can search it like the built-in servers.

    Assembles the endpoint URL for the chosen platform, verifies it with an SRU
    explain probe, confirms a working record schema with a test search, and on
    success saves it to ~/.sru-mcp/user_servers.json and caches its
    capabilities. On failure nothing is saved, and the reason is reported.
    Credentials, if supplied, are used only for the probe and are never stored.

    After adding, use the key with sru_search_books, sru_search, sru_explain,
    sru_list_indexes, etc. The added server works immediately in this session
    (no restart needed for the target itself).

    A common failure is not a bug: many institutions have not enabled SRU, or do
    not expose the endpoint publicly. For Alma specifically, SRU is off by
    default and must be turned on institution-side.

    Run sru_list_platforms first to see which inputs each platform needs.
    """
    inputs = {
        "domain": domain,
        "institution_code": institution_code,
        "host": host,
        "port": port,
        "database": database,
        "dbname": dbname,
        "scheme": scheme,
        "base_url": base_url,
    }
    inputs = {k: v for k, v in inputs.items() if v not in (None, "")}

    # 1. Assemble the base URL from the platform template.
    try:
        url, advisories = targets.assemble_url(platform, inputs)
    except ValueError as exc:
        return f"**Could not build the URL:** {exc}"

    # 2. Resolve a unique registry key (never overwriting a shipped/user id).
    existing = [s["id"] for s in sru.all_servers()]
    resolved_key, key_err = targets.resolve_key(name, key, existing)
    if key_err:
        return f"**{key_err}**"

    advisory_block = (
        "\n".join(f"> note: {a}" for a in advisories) + "\n\n"
    ) if advisories else ""

    # 3. Probe with explain BEFORE registering anything. Force the platform's
    #    SRU version, since the server is not yet in the registry (it would
    #    otherwise default to 1.2, wrong for the 1.1 Koha/FOLIO endpoints).
    platform_version = (
        targets.PLATFORM_TEMPLATES.get(platform, {}).get("defaults", {}).get("version")
    )
    try:
        root = await sru.explain(url, username, password, version=platform_version)
        info = sru.parse_explain(root)
    except sru.SRUError as exc:
        return (
            f"{advisory_block}"
            f"**Probe failed — nothing was saved.** Tried `{url}`.\n\n"
            f"{exc}\n\n"
            f"Common causes: the institution has not enabled SRU, the endpoint is "
            f"not publicly reachable, or an input is off. For Alma, confirm the "
            f"domain and institution code and that SRU is turned on. Run "
            f"sru_list_platforms to review the required inputs."
        )

    # 4. Probe succeeded: choose a record schema, then CONFIRM it actually
    #    returns records with a tiny test search before baking it in. "Advertised
    #    in explain" does not guarantee "retrievable": some endpoints list a
    #    schema they then reject on retrieval (seen most with the 'other'
    #    platform, where there is no templated known-good default). A failed or
    #    inconclusive probe never blocks the add — a reachable server is still
    #    worth registering; the caller can set record_schema explicitly.
    advertised = [s.get("name", "") for s in info.get("schemas", []) if s.get("name")]
    guessed = targets.choose_default_schema(platform, advertised)
    candidates = targets.schema_candidates(platform, advertised, guessed)
    validated, status = await sru.validate_schema_for_retrieval(
        url, candidates, version=platform_version,
        username=username, password=password,
    )
    schema = validated or guessed
    if status == "confirmed":
        schema_line = f"- Record schema: {schema} (confirmed by a test search)\n"
    else:
        schema_line = f"- Record schema: {schema}\n"
        # Only caveat for 'other': the templated platforms carry a reliable
        # default, so an inconclusive probe there is not worth alarming about.
        if platform == "other":
            schema_line += (
                f"  > note: could not confirm a record schema with a test search; "
                f"using {schema}. If retrieval returns nothing, pass record_schema "
                f"explicitly to sru_search / sru_search_books.\n"
            )
    entry = targets.build_entry(resolved_key, name, platform, url, inputs, schema)
    saved, _ = targets.register_user_server(entry)

    profile = sru.cache_capabilities_from_explain(resolved_key, info)
    n_indexes = len(profile.get("indexes", {}))
    schemas = profile.get("schemas", [])
    # Many Alma profiles leave databaseInfo/title blank, so parse_explain yields
    # "Unknown"; fall back to the name the user supplied (as sru_explain does).
    title = info.get("title")
    if not title or title == "Unknown":
        title = name

    persist_note = "" if saved else (
        "\n\n> warning: the server is usable now but could not be written to "
        "~/.sru-mcp/user_servers.json, so it will not persist across a restart."
    )
    return (
        f"{advisory_block}"
        f"**Added `{resolved_key}` — {title}.**\n"
        f"- URL: `{url}`\n"
        f"- Platform: {platform}\n"
        f"{schema_line}"
        f"- Indexes discovered: {n_indexes}\n"
        f"- Supported schemas: {', '.join(schemas) if schemas else 'none listed'}\n\n"
        f'Try it: `sru_search_books(server="{resolved_key}", keyword="...")`.'
        f"{persist_note}"
    )


# ---------------------------------------------------------------------------
# Tool: sru_remove_target
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"openWorldHint": True})
def sru_remove_target(
    key: Annotated[
        str,
        Field(description="The id of the user-added server to remove (see sru_list_servers). "
                          "Built-in servers cannot be removed."),
    ],
) -> str:
    """Remove a server previously added with sru_add_target.

    Deletes the entry from ~/.sru-mcp/user_servers.json and drops its cached
    capabilities. Only user-added servers can be removed; the built-in servers
    (loc, dnb, pitt, etc.) are part of the tool and are refused.
    """
    if key in {s["id"] for s in sru.SERVERS}:
        return (
            f"**`{key}` is a built-in server and cannot be removed.** "
            f"Only servers added with sru_add_target can be removed."
        )
    removed, _ = targets.remove_user_server(key)
    if not removed:
        return (
            f"**No user-added server with id `{key}`.** "
            f"Run sru_list_servers to see what is registered."
        )
    sru.uncache_server(key)
    return f"**Removed `{key}`.** Its entry and cached capabilities are gone."


# ---------------------------------------------------------------------------
# Tool: sru_explain
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def sru_explain(
    server: Annotated[str, _SERVER_URL_FIELD],
    username: Annotated[str | None, Field(description="Optional HTTP basic auth username")] = None,
    password: Annotated[str | None, Field(description="Optional HTTP basic auth password")] = None,
) -> str:
    """Get capabilities of an SRU server: title, supported record schemas,
    server defaults, and a summary of available indexes.

    Use this before searching to discover what indexes and schemas the server
    supports. Follow up with sru_list_indexes for a detailed index table.
    """
    try:
        root = await sru.explain(_resolve_url(server), username, password)
        info = sru.parse_explain(root)
        # Many Alma institutions leave the SRU profile's databaseInfo/title blank,
        # so explain yields no title even though schemas/indexes parse fine. Fall
        # back to the configured server name (or its URL) rather than "Unknown".
        if not info.get("title") or info["title"] == "Unknown":
            rec = sru.get_server(server)
            if rec and rec.get("name"):
                info["title"] = rec["name"]
            else:
                info["title"] = _resolve_url(server)
        return sru.format_explain_markdown(info)
    except sru.SRUError as exc:
        return f"**Error:** {exc}"


# ---------------------------------------------------------------------------
# Tool: sru_list_indexes
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def sru_list_indexes(
    server: Annotated[str, _SERVER_URL_FIELD],
    filter_text: Annotated[
        str | None,
        Field(description="Optional text to filter index names or titles (case-insensitive)"),
    ] = None,
    username: Annotated[str | None, Field(description="Optional HTTP basic auth username")] = None,
    password: Annotated[str | None, Field(description="Optional HTTP basic auth password")] = None,
) -> str:
    """List all search indexes available on an SRU server.

    Returns a markdown table with context set, index name, and title.
    Use filter_text to narrow results (e.g., 'title', 'author', 'subject').

    Index names from this table can be used directly in CQL queries passed
    to sru_search (e.g., 'dc.title = "Hamlet"').
    """
    try:
        root = await sru.explain(_resolve_url(server), username, password)
        info = sru.parse_explain(root)
        return sru.format_indexes_markdown(info["indexes"], filter_text)
    except sru.SRUError as exc:
        return f"**Error:** {exc}"


# ---------------------------------------------------------------------------
# Tool: sru_search
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def sru_search(
    server: Annotated[str, _SERVER_URL_FIELD],
    cql_query: Annotated[
        str,
        Field(
            description=(
                "CQL (Contextual Query Language) query string. "
                'Examples: \'dc.title = "Moby Dick"\', '
                '\'dc.creator = "Melville" AND dc.date = "1851"\', '
                '\'bath.isbn = "9780142437247"\'. '
                "Use sru_list_indexes to discover available index names."
            )
        ),
    ],
    max_records: Annotated[
        int,
        Field(description="Maximum number of records to return (1–100)", ge=1, le=100),
    ] = 10,
    start_record: Annotated[
        int,
        Field(description="1-based index of the first record to return (for pagination)", ge=1),
    ] = 1,
    record_schema: Annotated[
        str | None,
        Field(description="Record schema to request (e.g., 'dc', 'marcxml', 'mods'). "
                          "If omitted, the server's default schema from servers.json "
                          "is used. Use sru_explain to see supported schemas."),
    ] = None,
    username: Annotated[str | None, Field(description="Optional HTTP basic auth username")] = None,
    password: Annotated[str | None, Field(description="Optional HTTP basic auth password")] = None,
) -> str:
    """Execute a raw CQL query against an SRU server and return matching records.

    Returns a markdown summary of results including title, author, publisher,
    year, ISBN, subjects, and language for each record.

    For pagination, increment start_record by max_records on each call.
    The response includes the total number of matching records.
    """
    schema = record_schema or sru.server_default_schema(server)
    try:
        root = await sru.search_retrieve(
            _resolve_url(server), cql_query, max_records, start_record,
            schema, username, password,
        )
        results = sru.parse_search_results(root)
        return sru.format_search_results_markdown(results)
    except sru.SRUError as exc:
        return f"**Error:** {exc}"


# ---------------------------------------------------------------------------
# Tool: sru_search_books
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def sru_search_books(
    server: Annotated[str, _SERVER_URL_FIELD],
    title: Annotated[str | None, Field(description="Book title or partial title")] = None,
    author: Annotated[str | None, Field(description="Author name")] = None,
    isbn: Annotated[str | None, Field(description="ISBN (10 or 13 digits)")] = None,
    subject: Annotated[str | None, Field(description="Subject or topic keyword")] = None,
    publisher: Annotated[str | None, Field(description="Publisher name")] = None,
    year: Annotated[str | None, Field(description="Publication year (e.g., '2001')")] = None,
    keyword: Annotated[str | None, Field(description="General keyword search across all fields")] = None,
    max_records: Annotated[
        int,
        Field(description="Maximum number of records to return (1–100)", ge=1, le=100),
    ] = 10,
    start_record: Annotated[
        int,
        Field(description="1-based index of the first record to return (for pagination)", ge=1),
    ] = 1,
    record_schema: Annotated[
        str | None,
        Field(description="Record schema to request (e.g., 'dc', 'marcxml', 'oai_dc'). "
                          "If omitted, the server's default schema from servers.json "
                          "is used (e.g. oai_dc for DNB, marcxml for Alma)."),
    ] = None,
    sort: Annotated[
        str | None,
        Field(description="Optional CQL sort spec, e.g. 'alma.title/sort.ascending'. "
                          "If omitted, Alma servers default to relevance "
                          "(alma.rank/sort.descending); other servers use their own "
                          "default ordering. Pass an empty string to disable sorting."),
    ] = None,
) -> str:
    """Search an SRU library catalog by common bibliographic fields.

    Provide any combination of title, author, isbn, subject, publisher, year,
    or keyword. Multiple fields are AND-combined. At least one field is required.

    Returns a markdown summary of matching records.

    Example (German National Library, works out of the box):
      server = "dnb"
      keyword = "Goethe"

    Note: field-based search uses each server's index set (from servers.json
    "default_index"). For Ex Libris Alma servers (e.g. "pitt") that means the
    alma.* indexes are used automatically, results are relevance-sorted by
    default, and the record schema defaults to each server's configured schema.
    """
    index_set = sru.server_default_index(server)
    try:
        cql = sru.build_cql(
            index_set=index_set,
            sort=sort,
            title=title,
            author=author,
            isbn=isbn,
            subject=subject,
            publisher=publisher,
            year=year,
            keyword=keyword,
        )
    except ValueError as exc:
        return f"**Error:** {exc}"

    # Resolve the record schema from the server config unless the caller forced one.
    schema = record_schema or sru.server_default_schema(server)
    # Alma caps maximumRecords at 50 and errors above it.
    if index_set == "alma" and max_records > 50:
        max_records = 50

    # Advisory capability validation: if this server has been discovered (via
    # sru_refresh_capabilities), check that each mapped index actually exists on
    # it, and surface a warning rather than letting a missing index produce a
    # silent zero-result. Never blocks: undiscovered servers (None) are skipped,
    # and the universal keyword index (cql.anywhere) is not a catalog index so it
    # is never warned on.
    warnings: list[str] = []
    planned = sru.fields_to_indexes(index_set, {
        "title": title, "author": author, "isbn": isbn, "subject": subject,
        "publisher": publisher, "year": year, "keyword": keyword,
    })
    for field, idx in planned.items():
        if idx == "cql.anywhere":
            continue
        exists = sru.index_exists(server, idx)
        if exists is False:
            warnings.append(
                f"index `{idx}` (for {field}) is not in {server}'s discovered "
                f"capabilities — results may be empty. Run sru_refresh_capabilities "
                f"or check sru_list_indexes."
            )
    warning_block = ("> ⚠️ " + "\n> ".join(warnings) + "\n\n") if warnings else ""

    try:
        root = await sru.search_retrieve(
            _resolve_url(server), cql, max_records, start_record, schema,
        )
        results = sru.parse_search_results(root)
        md = sru.format_search_results_markdown(results)
        return f"{warning_block}**Query:** `{cql}`\n\n{md}"
    except sru.SRUError as exc:
        return f"**Error:** {exc}"


# ---------------------------------------------------------------------------
# Tool: sru_scan
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def sru_scan(
    server: Annotated[str, _SERVER_URL_FIELD],
    scan_clause: Annotated[str, Field(description="CQL index and term to scan, e.g. 'dc.title = moby'")],
    max_terms: Annotated[
        int,
        Field(description="Maximum number of index terms to return (1–100)", ge=1, le=100),
    ] = 20,
    response_position: Annotated[
        int,
        Field(description="Position of the scan clause term within the returned list (1-based)", ge=1),
    ] = 1,
    username: Annotated[str | None, Field(description="Optional HTTP basic auth username")] = None,
    password: Annotated[str | None, Field(description="Optional HTTP basic auth password")] = None,
) -> str:
    """Browse index terms near a given term on an SRU server (scan operation).

    Returns a list of index terms and their record counts, which is useful
    for exploring available values before running a full search.

    Example: scan dc.title = "moby" to see title terms alphabetically near "moby".
    """
    try:
        root = await sru.scan(
            _resolve_url(server), scan_clause, max_terms, response_position,
            username, password,
        )
        terms = sru.parse_scan_results(root)
        if not terms:
            return "No terms found."
        lines = ["| Term | Count |", "|------|-------|"]
        for t in terms:
            count = t.get("count", "")
            lines.append(f"| {t['term']} | {count} |")
        return "\n".join(lines)
    except sru.SRUError as exc:
        return f"**Error:** {exc}"


# ---------------------------------------------------------------------------
# Tool: sru_refresh_capabilities
# ---------------------------------------------------------------------------

@mcp.tool(annotations={"openWorldHint": True})
async def sru_refresh_capabilities(
    server: Annotated[str, _SERVER_URL_FIELD],
    username: Annotated[str | None, Field(description="Optional HTTP basic auth username")] = None,
    password: Annotated[str | None, Field(description="Optional HTTP basic auth password")] = None,
) -> str:
    """Fetch a server's explain document and cache its capabilities on disk.

    This discovers and stores which indexes the server exposes (and which are
    sortable) plus its supported record schemas. Once cached, sru_search_books
    can warn when a planned search uses an index the server doesn't actually
    expose, instead of returning a silent zero-result. The cache persists across
    restarts; re-run this when a catalog's configuration changes.

    Discovery is an enhancement, not a requirement: searches work the same
    whether or not a server has been refreshed. (sru_add_target caches
    capabilities automatically when it registers a server, so a manual refresh
    is only needed later, if a catalog changes.)
    """
    profile = await sru.discover_capabilities(_resolve_url(server), username, password)
    if profile is None:
        return (
            f"**Could not discover capabilities for `{server}`.** The explain "
            f"request failed or returned nothing usable. Searches will continue "
            f"to use the configured defaults from servers.json."
        )
    n_indexes = len(profile.get("indexes", {}))
    sortable = profile.get("sortable", [])
    schemas = profile.get("schemas", [])
    lines = [
        f"**Cached capabilities for `{server}`** ({profile.get('title') or server}).",
        f"- Indexes discovered: {n_indexes}",
        f"- Sortable indexes: {', '.join(f'`{s}`' for s in sortable) if sortable else 'none advertised'}",
        f"- Supported schemas: {', '.join(schemas) if schemas else 'none listed'}",
        f"- Fetched: {profile.get('fetched_at', 'unknown')}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
