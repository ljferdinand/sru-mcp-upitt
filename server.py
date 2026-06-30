"""SRU MCP Server

Provides tools for searching library catalogs via the SRU
(Search/Retrieve via URL) protocol.

Servers are configured in servers.json. Use sru_list_servers to see all
available servers, or pass any SRU server URL directly.

Run:
  python server.py
"""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

import sru

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
        f"or sru_search for raw CQL queries. Available servers: {_SERVER_HINT}."
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

    Returns a table with ID, name, URL, default schema, and notes for each
    server. Pass the ID or URL to other sru_* tools.
    """
    lines = ["| ID | Name | URL | Schema | Notes |",
             "|----|------|-----|--------|-------|"]
    for s in sru.SERVERS:
        lines.append(
            f"| {s['id']} | {s['name']} | {s['url']} "
            f"| {s['default_schema']} | {s['notes']} |"
        )
    return "\n".join(lines)


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
    whether or not a server has been refreshed.
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
