"""Unit tests for server.py — tool logic and URL resolution."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import sru

# Import the module — this also validates that it loads without error
import server


# =====================================================================
# _resolve_url
# =====================================================================

class TestResolveUrl:
    def test_known_id(self):
        assert server._resolve_url("loc") == "https://lx2.loc.gov/sru/lcdb"

    def test_unknown_id_passed_through(self):
        assert server._resolve_url("http://custom.example/sru") == "http://custom.example/sru"

    def test_all_known_ids_resolve(self):
        for sid in sru.KNOWN_SERVERS:
            url = server._resolve_url(sid)
            assert url.startswith("http")


# =====================================================================
# sru_list_servers
# =====================================================================

class TestListServers:
    def test_returns_markdown_table(self):
        result = server.sru_list_servers()
        assert "| ID |" in result
        assert "| loc |" in result
        assert "Library of Congress" in result

    def test_all_servers_present(self):
        result = server.sru_list_servers()
        for s in sru.SERVERS:
            assert s["id"] in result


# =====================================================================
# sru_explain tool
# =====================================================================

class TestSruExplainTool:
    @pytest.mark.asyncio
    async def test_returns_markdown_on_success(self):
        mock_root = {"zs:record": {"zs:recordData": {"explain": {
            "databaseInfo": {"title": "Test Lib"},
        }}}}
        with patch("sru.explain", new_callable=AsyncMock, return_value=mock_root):
            result = await server.sru_explain("loc")
        assert "## Test Lib" in result

    @pytest.mark.asyncio
    async def test_returns_error_on_failure(self):
        with patch("sru.explain", new_callable=AsyncMock,
                   side_effect=sru.SRUError("connection refused")):
            result = await server.sru_explain("http://bad.example")
        assert "**Error:**" in result
        assert "connection refused" in result

    @pytest.mark.asyncio
    async def test_resolves_server_id(self):
        mock_root = {"zs:record": {"zs:recordData": {"explain": {
            "databaseInfo": {"title": "LOC"},
        }}}}
        with patch("sru.explain", new_callable=AsyncMock, return_value=mock_root) as mock:
            await server.sru_explain("loc")
        mock.assert_called_once_with("https://lx2.loc.gov/sru/lcdb", None, None)


# =====================================================================
# sru_list_indexes tool
# =====================================================================

class TestSruListIndexesTool:
    @pytest.mark.asyncio
    async def test_returns_index_table(self):
        mock_root = {"zs:record": {"zs:recordData": {"explain": {
            "databaseInfo": {"title": "Lib"},
            "indexInfo": {"index": [
                {"title": "Title", "map": {"name": {"@set": "dc", "#text": "title"}}},
            ]},
        }}}}
        with patch("sru.explain", new_callable=AsyncMock, return_value=mock_root):
            result = await server.sru_list_indexes("loc")
        assert "| dc | title | Title |" in result

    @pytest.mark.asyncio
    async def test_forwards_credentials(self):
        mock_root = {"zs:record": {"zs:recordData": {"explain": {
            "databaseInfo": {"title": "Lib"},
        }}}}
        with patch("sru.explain", new_callable=AsyncMock, return_value=mock_root) as mock:
            await server.sru_list_indexes("loc", username="user", password="pass")
        mock.assert_called_once_with("https://lx2.loc.gov/sru/lcdb", "user", "pass")


# =====================================================================
# sru_search tool
# =====================================================================

class TestSruSearchTool:
    @pytest.mark.asyncio
    async def test_returns_formatted_results(self):
        mock_root = {
            "zs:numberOfRecords": "1",
            "zs:records": {"zs:record": [{
                "zs:recordSchema": "dc",
                "zs:recordData": {
                    "srw_dc:dc": {"dc:title": "Test Book"},
                },
            }]},
        }
        with patch("sru.search_retrieve", new_callable=AsyncMock, return_value=mock_root):
            result = await server.sru_search("loc", 'dc.title = "Test"')
        assert "Test Book" in result

    @pytest.mark.asyncio
    async def test_error_handling(self):
        with patch("sru.search_retrieve", new_callable=AsyncMock,
                   side_effect=sru.SRUError("bad query")):
            result = await server.sru_search("loc", "bad query")
        assert "**Error:**" in result


# =====================================================================
# sru_search_books tool
# =====================================================================

class TestSruSearchBooksTool:
    @pytest.mark.asyncio
    async def test_builds_cql_and_searches(self):
        mock_root = {
            "zs:numberOfRecords": "1",
            "zs:records": {"zs:record": [{
                "zs:recordSchema": "dc",
                "zs:recordData": {
                    "srw_dc:dc": {"dc:title": "Hamlet"},
                },
            }]},
        }
        with patch("sru.search_retrieve", new_callable=AsyncMock, return_value=mock_root) as mock:
            result = await server.sru_search_books("loc", title="Hamlet")
        assert "Hamlet" in result
        assert "**Query:**" in result
        # Verify CQL was built and passed
        call_args = mock.call_args
        assert "dc.title = Hamlet" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_no_fields_returns_error(self):
        result = await server.sru_search_books("loc")
        assert "**Error:**" in result
        assert "At least one" in result

    @pytest.mark.asyncio
    async def test_resolves_server_id(self):
        mock_root = {
            "zs:numberOfRecords": "0",
            "zs:records": {},
        }
        with patch("sru.search_retrieve", new_callable=AsyncMock, return_value=mock_root) as mock:
            await server.sru_search_books("dnb", keyword="test")
        assert mock.call_args[0][0] == "http://services.dnb.de/sru/dnb"
