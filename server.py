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
                          "Defaults to 'marcxml'. Use sru_explain to see supported schemas."),
    ] = "marcxml",
    username: Annotated[str | None, Field(description="Optional HTTP basic auth username")] = None,
    password: Annotated[str | None, Field(description="Optional HTTP basic auth password")] = None,
) -> str:
    """Execute a raw CQL query against an SRU server and return matching records.

    Returns a markdown summary of results including title, author, publisher,
    year, ISBN, subjects, and language for each record.

    For pagination, increment start_record by max_records on each call.
    The response includes the total number of matching records.
    """
    try:
        root = await sru.search_retrieve(
            _resolve_url(server), cql_query, max_records, start_record,
            record_schema, username, password,
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
        Field(description="Record schema to request (e.g., 'dc', 'marcxml'). "
                          "Defaults to 'marcxml'."),
    ] = "marcxml",
) -> str:
    """Search an SRU library catalog by common bibliographic fields.

    Provide any combination of title, author, isbn, subject, publisher, year,
    or keyword. Multiple fields are AND-combined. At least one field is required.

    Returns a markdown summary of matching records.

    Example using the Library of Congress:
      server = "loc"
      title = "Moby Dick"
      author = "Melville"
    """
    try:
        cql = sru.build_cql(
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

    try:
        root = await sru.search_retrieve(
            _resolve_url(server), cql, max_records, start_record, record_schema,
        )
        results = sru.parse_search_results(root)
        md = sru.format_search_results_markdown(results)
        return f"**Query:** `{cql}`\n\n{md}"
    except sru.SRUError as exc:
        return f"**Error:** {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
