"""Unit tests for sru.py — all pure functions, no network calls."""

from __future__ import annotations

import pytest
import httpx
import respx

import sru


# =====================================================================
# Utility helpers
# =====================================================================

class TestFirst:
    def test_returns_first_matching_key(self):
        d = {"zs:foo": 1, "foo": 2}
        assert sru._first(d, "zs:foo", "foo") == 1

    def test_falls_back_to_second_key(self):
        d = {"foo": 2}
        assert sru._first(d, "zs:foo", "foo") == 2

    def test_returns_none_when_no_match(self):
        assert sru._first({}, "a", "b") is None

    def test_returns_falsy_values(self):
        d = {"key": 0}
        assert sru._first(d, "key") == 0

        d2 = {"key": ""}
        assert sru._first(d2, "key") == ""


class TestText:
    def test_returns_plain_string(self):
        assert sru._text({"title": "Hello"}, "title") == "Hello"

    def test_unwraps_text_dict(self):
        assert sru._text({"title": {"#text": "Hello", "@lang": "en"}}, "title") == "Hello"

    def test_unwraps_value_attr(self):
        assert sru._text({"x": {"@value": "V"}}, "x") == "V"

    def test_returns_first_list_element(self):
        assert sru._text({"x": ["first", "second"]}, "x") == "first"

    def test_returns_none_for_missing(self):
        assert sru._text({}, "missing") is None

    def test_skips_none_values(self):
        assert sru._text({"a": None, "b": "ok"}, "a", "b") == "ok"


class TestEnsureList:
    def test_list_passthrough(self):
        lst = [1, 2]
        assert sru._ensure_list(lst) is lst

    def test_none_returns_empty(self):
        assert sru._ensure_list(None) == []

    def test_wraps_scalar(self):
        assert sru._ensure_list("hello") == ["hello"]

    def test_wraps_dict(self):
        d = {"a": 1}
        assert sru._ensure_list(d) == [d]


class TestListify:
    def test_list_input(self):
        assert sru._listify([1, 2, 3]) == ["1", "2", "3"]

    def test_scalar_input(self):
        assert sru._listify("hello") == ["hello"]

    def test_dict_input(self):
        assert sru._listify({"a": 1}) == ["{'a': 1}"]


class TestJoin:
    def test_list(self):
        assert sru._join(["a", "b", "c"]) == "a; b; c"

    def test_scalar(self):
        assert sru._join("hello") == "hello"

    def test_single_element_list(self):
        assert sru._join(["only"]) == "only"


class TestSubfield:
    def test_finds_matching_code(self):
        sfs = [{"@code": "a", "#text": "Hello"}, {"@code": "b", "#text": "World"}]
        assert sru._subfield(sfs, "a") == "Hello"
        assert sru._subfield(sfs, "b") == "World"

    def test_returns_none_for_missing(self):
        sfs = [{"@code": "a", "#text": "Hello"}]
        assert sru._subfield(sfs, "z") is None

    def test_empty_list(self):
        assert sru._subfield([], "a") is None


# =====================================================================
# Server registry
# =====================================================================

class TestServerRegistry:
    def test_servers_loaded(self):
        assert len(sru.SERVERS) > 0

    def test_known_servers_populated(self):
        assert "loc" in sru.KNOWN_SERVERS

    def test_get_server_by_id(self):
        s = sru.get_server("loc")
        assert s is not None
        assert s["id"] == "loc"
        assert s["url"] == "https://lx2.loc.gov/sru/lcdb"

    def test_get_server_by_url(self):
        s = sru.get_server("https://lx2.loc.gov/sru/lcdb")
        assert s is not None
        assert s["id"] == "loc"

    def test_get_server_unknown(self):
        assert sru.get_server("nonexistent") is None

    def test_all_servers_have_required_keys(self):
        required = {"id", "name", "url", "version", "default_schema", "default_index", "notes"}
        for s in sru.SERVERS:
            assert required.issubset(s.keys()), f"Server {s.get('id')} missing keys"


# =====================================================================
# CQL builder
# =====================================================================

class TestBuildCQL:
    def test_single_field(self):
        assert sru.build_cql(title="Hamlet") == 'dc.title = Hamlet'

    def test_quoted_term_with_spaces(self):
        assert sru.build_cql(title="Moby Dick") == 'dc.title = "Moby Dick"'

    def test_multiple_fields_and_joined(self):
        cql = sru.build_cql(title="Hamlet", author="Shakespeare")
        assert "dc.title = Hamlet" in cql
        assert "dc.creator = Shakespeare" in cql
        assert " AND " in cql

    def test_none_fields_skipped(self):
        cql = sru.build_cql(title="Hamlet", author=None, isbn=None)
        assert cql == "dc.title = Hamlet"

    def test_empty_string_fields_skipped(self):
        cql = sru.build_cql(title="Hamlet", author="")
        assert cql == "dc.title = Hamlet"

    def test_all_field_types(self):
        cql = sru.build_cql(isbn="123456")
        assert cql == "bath.isbn = 123456"

        cql = sru.build_cql(keyword="test")
        assert cql == "cql.anywhere = test"

    def test_unknown_field_raises(self):
        with pytest.raises(ValueError, match="Unknown search field"):
            sru.build_cql(badfield="test")

    def test_no_fields_raises(self):
        with pytest.raises(ValueError, match="At least one"):
            sru.build_cql()

    def test_all_none_raises(self):
        with pytest.raises(ValueError, match="At least one"):
            sru.build_cql(title=None, author=None)


# =====================================================================
# SRU operations (mocked HTTP)
# =====================================================================

EXPLAIN_XML = """\
<?xml version="1.0"?>
<zs:explainResponse xmlns:zs="http://www.loc.gov/zing/srw/">
  <zs:version>1.1</zs:version>
  <zs:record>
    <zs:recordSchema>http://explain.z3950.org/dtd/2.0/</zs:recordSchema>
    <zs:recordPacking>xml</zs:recordPacking>
    <zs:recordData>
      <explain xmlns="http://explain.z3950.org/dtd/2.0/">
        <databaseInfo>
          <title>Test Catalog</title>
          <description lang="en" primary="true">A test catalog.</description>
        </databaseInfo>
        <indexInfo>
          <set identifier="info:srw/cql-context-set/1/dc-v1.1" name="dc"/>
          <index id="4">
            <title>Title</title>
            <map><name set="dc">title</name></map>
          </index>
          <index id="1003">
            <title>Creator</title>
            <map>
              <name set="dc">creator</name>
            </map>
          </index>
        </indexInfo>
        <schemaInfo>
          <schema identifier="http://www.loc.gov/MARC21/slim" name="marcxml">
            <title>MARCXML v 1.1</title>
          </schema>
          <schema identifier="http://purl.org/dc/elements/1.1/" name="dc">
            <title>Dublin Core v 1.1</title>
          </schema>
        </schemaInfo>
      </explain>
    </zs:recordData>
  </zs:record>
</zs:explainResponse>
"""

SEARCH_XML_MARCXML = """\
<?xml version="1.0"?>
<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">
  <zs:version>1.1</zs:version>
  <zs:numberOfRecords>1</zs:numberOfRecords>
  <zs:records>
    <zs:record>
      <zs:recordSchema>marcxml</zs:recordSchema>
      <zs:recordPacking>xml</zs:recordPacking>
      <zs:recordData>
        <record xmlns="http://www.loc.gov/MARC21/slim">
          <leader>01234cam a2200000 a 4500</leader>
          <controlfield tag="008">830101s1851    nyu           000 1 eng  </controlfield>
          <datafield tag="020" ind1=" " ind2=" ">
            <subfield code="a">0142437247 (pbk.)</subfield>
          </datafield>
          <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Melville, Herman,</subfield>
          </datafield>
          <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">Moby Dick;</subfield>
            <subfield code="b">or, The whale /</subfield>
          </datafield>
          <datafield tag="260" ind1=" " ind2=" ">
            <subfield code="b">Harper &amp; Brothers</subfield>
            <subfield code="c">1851.</subfield>
          </datafield>
          <datafield tag="300" ind1=" " ind2=" ">
            <subfield code="a">xxiii, 635 p.</subfield>
          </datafield>
          <datafield tag="650" ind1=" " ind2="0">
            <subfield code="a">Whaling ships</subfield>
          </datafield>
          <datafield tag="650" ind1=" " ind2="0">
            <subfield code="a">Sea stories</subfield>
          </datafield>
          <datafield tag="700" ind1="1" ind2=" ">
            <subfield code="a">Rockwell, Norman,</subfield>
          </datafield>
        </record>
      </zs:recordData>
    </zs:record>
  </zs:records>
</zs:searchRetrieveResponse>
"""

SEARCH_XML_DC = """\
<?xml version="1.0"?>
<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">
  <zs:version>1.1</zs:version>
  <zs:numberOfRecords>1</zs:numberOfRecords>
  <zs:records>
    <zs:record>
      <zs:recordSchema>dc</zs:recordSchema>
      <zs:recordPacking>xml</zs:recordPacking>
      <zs:recordData>
        <srw_dc:dc xmlns:srw_dc="info:srw/schema/1/dc-schema"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>Moby Dick</dc:title>
          <dc:creator>Melville, Herman</dc:creator>
          <dc:subject>Whaling</dc:subject>
          <dc:publisher>Harper</dc:publisher>
          <dc:date>1851</dc:date>
          <dc:language>eng</dc:language>
        </srw_dc:dc>
      </zs:recordData>
    </zs:record>
  </zs:records>
</zs:searchRetrieveResponse>
"""

DIAGNOSTIC_XML = """\
<?xml version="1.0"?>
<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">
  <zs:version>1.1</zs:version>
  <zs:numberOfRecords>0</zs:numberOfRecords>
  <zs:diagnostics>
    <diag:diagnostic xmlns:diag="http://www.loc.gov/zing/srw/diagnostic/">
      <diag:message>Unsupported index</diag:message>
    </diag:diagnostic>
  </zs:diagnostics>
</zs:searchRetrieveResponse>
"""


@pytest.fixture
def mock_sru_server():
    """Fixture that provides an respx router for mocking HTTP calls."""
    with respx.mock(assert_all_mocked=False) as router:
        yield router


class TestExplain:
    @pytest.mark.asyncio
    async def test_explain_parses_response(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text=EXPLAIN_XML,
                                        headers={"content-type": "text/xml"})
        )
        root = await sru.explain("http://test.example/sru")
        info = sru.parse_explain(root)

        assert info["title"] == "Test Catalog"
        assert info["description"] == "A test catalog."
        assert len(info["schemas"]) == 2
        assert info["schemas"][0]["name"] == "marcxml"
        assert info["schemas"][1]["name"] == "dc"
        assert len(info["indexes"]) == 2

    @pytest.mark.asyncio
    async def test_explain_timeout(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            side_effect=httpx.ConnectTimeout("timed out")
        )
        with pytest.raises(sru.SRUError, match="timed out"):
            await sru.explain("http://test.example/sru")

    @pytest.mark.asyncio
    async def test_explain_http_error(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(500)
        )
        with pytest.raises(sru.SRUError, match="HTTP 500"):
            await sru.explain("http://test.example/sru")

    @pytest.mark.asyncio
    async def test_explain_empty_response(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text="   ",
                                        headers={"content-type": "text/xml"})
        )
        with pytest.raises(sru.SRUError, match="Empty response"):
            await sru.explain("http://test.example/sru")

    @pytest.mark.asyncio
    async def test_explain_non_xml(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text="<html>not xml</html broken",
                                        headers={"content-type": "text/html"})
        )
        with pytest.raises(sru.SRUError, match="non-XML"):
            await sru.explain("http://test.example/sru")


class TestSearchRetrieve:
    @pytest.mark.asyncio
    async def test_search_marcxml(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text=SEARCH_XML_MARCXML,
                                        headers={"content-type": "text/xml"})
        )
        root = await sru.search_retrieve("http://test.example/sru", 'dc.title = "Moby Dick"')
        results = sru.parse_search_results(root)

        assert results["total"] == 1
        rec = results["records"][0]
        assert "Moby Dick" in rec["title"]
        assert rec["author"] == ["Melville, Herman"]
        assert rec["isbn"] == ["0142437247"]
        assert rec["publisher"] == "Harper & Brothers"
        assert rec["year"] == "1851"
        assert rec["extent"] == "xxiii, 635 p."
        assert "Whaling ships" in rec["subject"]
        assert "Sea stories" in rec["subject"]
        assert rec["contributors"] == ["Rockwell, Norman"]
        assert rec["language"] == "eng"

    @pytest.mark.asyncio
    async def test_search_dublin_core(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text=SEARCH_XML_DC,
                                        headers={"content-type": "text/xml"})
        )
        root = await sru.search_retrieve("http://test.example/sru", 'dc.title = "Moby Dick"')
        results = sru.parse_search_results(root)

        assert results["total"] == 1
        rec = results["records"][0]
        assert rec["title"] == ["Moby Dick"]
        assert rec["author"] == ["Melville, Herman"]
        assert rec["publisher"] == ["Harper"]
        assert rec["date"] == ["1851"]
        assert rec["language"] == ["eng"]

    @pytest.mark.asyncio
    async def test_search_diagnostic_raises(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text=DIAGNOSTIC_XML,
                                        headers={"content-type": "text/xml"})
        )
        with pytest.raises(sru.SRUError, match="Unsupported index"):
            await sru.search_retrieve("http://test.example/sru", "bad.index = test")

    @pytest.mark.asyncio
    async def test_search_network_error(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            side_effect=httpx.ConnectError("refused")
        )
        with pytest.raises(sru.SRUError, match="Could not reach"):
            await sru.search_retrieve("http://test.example/sru", "dc.title = test")


# =====================================================================
# Response parsers — pure function tests
# =====================================================================

class TestParseExplain:
    def test_minimal_explain(self):
        root = {"zs:record": {"zs:recordData": {"explain": {
            "databaseInfo": {"title": "My Lib"},
        }}}}
        info = sru.parse_explain(root)
        assert info["title"] == "My Lib"
        assert info["schemas"] == []
        assert info["indexes"] == []

    def test_no_explain_block_uses_fallback(self):
        root = {}
        info = sru.parse_explain(root)
        assert info["title"] == "Unknown"

    def test_description_as_dict(self):
        root = {"zs:record": {"zs:recordData": {"explain": {
            "databaseInfo": {
                "title": "Lib",
                "description": {"#text": "A description", "@lang": "en"},
            },
        }}}}
        info = sru.parse_explain(root)
        assert info["description"] == "A description"

    def test_schemas_parsed(self):
        root = {"zs:record": {"zs:recordData": {"explain": {
            "databaseInfo": {"title": "Lib"},
            "schemaInfo": {"schema": [
                {"@name": "dc", "@identifier": "urn:dc", "title": "Dublin Core"},
            ]},
        }}}}
        info = sru.parse_explain(root)
        assert len(info["schemas"]) == 1
        assert info["schemas"][0]["name"] == "dc"


class TestParseSearchResults:
    def test_zero_results(self):
        root = {"zs:numberOfRecords": "0"}
        results = sru.parse_search_results(root)
        assert results["total"] == 0
        assert results["records"] == []

    def test_invalid_total_defaults_to_zero(self):
        root = {"zs:numberOfRecords": "notanumber"}
        results = sru.parse_search_results(root)
        assert results["total"] == 0

    def test_next_position(self):
        root = {
            "zs:numberOfRecords": "100",
            "zs:nextRecordPosition": "11",
            "zs:records": {"zs:record": []},
        }
        results = sru.parse_search_results(root)
        assert results["next_position"] == 11

    def test_string_record_data(self):
        root = {
            "zs:numberOfRecords": "1",
            "zs:records": {"zs:record": [
                {"zs:recordSchema": "raw", "zs:recordData": "plain text data"},
            ]},
        }
        results = sru.parse_search_results(root)
        assert results["records"][0]["raw"] == "plain text data"


class TestParseMarcxml:
    def test_title_245(self):
        marc = {"datafield": [
            {"@tag": "245", "subfield": [
                {"@code": "a", "#text": "Moby Dick;"},
                {"@code": "b", "#text": "or, The whale /"},
            ]},
        ]}
        result = sru._parse_marcxml(marc, "marcxml")
        assert result["title"] == "Moby Dick; or, The whale"

    def test_isbn_020_strips_qualifier(self):
        marc = {"datafield": [
            {"@tag": "020", "subfield": {"@code": "a", "#text": "0142437247 (pbk.)"}},
        ]}
        result = sru._parse_marcxml(marc, "")
        assert result["isbn"] == ["0142437247"]

    def test_260_264_first_wins(self):
        marc = {"datafield": [
            {"@tag": "260", "subfield": [
                {"@code": "b", "#text": "Publisher A,"},
                {"@code": "c", "#text": "2000."},
            ]},
            {"@tag": "264", "subfield": [
                {"@code": "b", "#text": "Publisher B,"},
                {"@code": "c", "#text": "2001."},
            ]},
        ]}
        result = sru._parse_marcxml(marc, "")
        assert result["publisher"] == "Publisher A"
        assert result["year"] == "2000"

    def test_control_field_008_year_and_language(self):
        marc = {
            "datafield": [],
            "controlfield": [
                {"@tag": "008", "#text": "830101s1851    nyu           000 1 eng  "},
            ],
        }
        result = sru._parse_marcxml(marc, "")
        assert result["year"] == "1851"
        assert result["language"] == "eng"

    def test_control_field_008_does_not_override_datafield_year(self):
        marc = {
            "datafield": [
                {"@tag": "260", "subfield": [
                    {"@code": "c", "#text": "1999."},
                ]},
            ],
            "controlfield": [
                {"@tag": "008", "#text": "830101s1851    nyu           000 1 eng  "},
            ],
        }
        result = sru._parse_marcxml(marc, "")
        assert result["year"] == "1999"

    def test_subjects_650(self):
        marc = {"datafield": [
            {"@tag": "650", "subfield": {"@code": "a", "#text": "Whaling."}},
            {"@tag": "650", "subfield": {"@code": "a", "#text": "Sea stories."}},
        ]}
        result = sru._parse_marcxml(marc, "")
        assert result["subject"] == ["Whaling", "Sea stories"]

    def test_notes_500_520(self):
        marc = {"datafield": [
            {"@tag": "500", "subfield": {"@code": "a", "#text": "A general note"}},
            {"@tag": "520", "subfield": {"@code": "a", "#text": "A summary"}},
        ]}
        result = sru._parse_marcxml(marc, "")
        assert result["notes"] == ["A general note", "A summary"]

    def test_edition_250(self):
        marc = {"datafield": [
            {"@tag": "250", "subfield": {"@code": "a", "#text": "2nd ed."}},
        ]}
        result = sru._parse_marcxml(marc, "")
        assert result["edition"] == "2nd ed."

    def test_url_856(self):
        marc = {"datafield": [
            {"@tag": "856", "subfield": {"@code": "u", "#text": "http://example.com/book"}},
        ]}
        result = sru._parse_marcxml(marc, "")
        assert result["urls"] == ["http://example.com/book"]

    def test_empty_marc(self):
        result = sru._parse_marcxml({}, "marcxml")
        assert result == {"schema": "marcxml"}


class TestParseDublinCore:
    def test_basic_dc_fields(self):
        dc = {
            "dc:title": "A Title",
            "dc:creator": "An Author",
            "dc:subject": ["Subj1", "Subj2"],
        }
        result = sru._parse_dublin_core(dc, "dc")
        assert result["title"] == ["A Title"]
        assert result["author"] == ["An Author"]
        assert result["subject"] == ["Subj1", "Subj2"]

    def test_unprefixed_keys(self):
        dc = {"title": "Plain Title", "creator": "Plain Author"}
        result = sru._parse_dublin_core(dc, "dc")
        assert result["title"] == ["Plain Title"]
        assert result["author"] == ["Plain Author"]

    def test_empty_dc(self):
        result = sru._parse_dublin_core({}, "dc")
        assert result == {"schema": "dc"}


# =====================================================================
# Formatting helpers
# =====================================================================

class TestFormatExplainMarkdown:
    def test_basic_output(self):
        info = {
            "title": "My Library",
            "description": "A great library",
            "schemas": [{"name": "dc", "identifier": "urn:dc", "title": "Dublin Core"}],
            "defaults": {"numberOfRecords": "10"},
            "indexes": [{"set": "dc", "name": "title", "title": "Title"}],
        }
        md = sru.format_explain_markdown(info)
        assert "## My Library" in md
        assert "A great library" in md
        assert "Dublin Core" in md
        assert "numberOfRecords: 10" in md
        assert "1 total" in md

    def test_no_optional_sections(self):
        info = {"title": "Lib", "description": "", "schemas": [], "defaults": {}, "indexes": []}
        md = sru.format_explain_markdown(info)
        assert "## Lib" in md
        assert "Supported Record Schemas" not in md
        assert "Server Defaults" not in md


class TestFormatIndexesMarkdown:
    def test_table_output(self):
        indexes = [
            {"set": "dc", "name": "title", "title": "Title"},
            {"set": "dc", "name": "creator", "title": "Creator"},
        ]
        md = sru.format_indexes_markdown(indexes)
        assert "| dc | title | Title |" in md
        assert "| dc | creator | Creator |" in md

    def test_filter(self):
        indexes = [
            {"set": "dc", "name": "title", "title": "Title"},
            {"set": "dc", "name": "creator", "title": "Creator"},
        ]
        md = sru.format_indexes_markdown(indexes, "title")
        assert "title" in md.lower()
        assert "creator" not in md.lower()

    def test_no_matches(self):
        indexes = [{"set": "dc", "name": "title", "title": "Title"}]
        md = sru.format_indexes_markdown(indexes, "zzzzz")
        assert "No indexes found" in md

    def test_empty_list(self):
        md = sru.format_indexes_markdown([])
        assert "No indexes found" in md


class TestFormatSearchResultsMarkdown:
    def test_zero_results(self):
        md = sru.format_search_results_markdown({"total": 0, "records": []})
        assert "No records found" in md

    def test_single_record(self):
        results = {
            "total": 1,
            "records": [{
                "title": "Test Book",
                "author": ["Author A"],
                "year": "2020",
                "isbn": ["123"],
                "subject": ["Subj"],
                "language": "eng",
                "publisher": "Pub Co",
                "extent": "300 p.",
                "schema": "marcxml",
            }],
        }
        md = sru.format_search_results_markdown(results)
        assert "**Found 1 record(s)**" in md
        assert "Test Book" in md
        assert "Author A" in md
        assert "2020" in md
        assert "123" in md
        assert "Subj" in md
        assert "eng" in md
        assert "Pub Co" in md
        assert "300 p." in md

    def test_record_without_optional_fields(self):
        results = {"total": 1, "records": [{"title": "Minimal", "schema": "dc"}]}
        md = sru.format_search_results_markdown(results)
        assert "Minimal" in md
        assert "Author" not in md


# =====================================================================
# Regression tests for the namespace-agnostic / list-safe explain parser
# (the live "Unknown / 0 indexes" failure: real servers vary in how they
# namespace the explain payload, and force_list wraps a lone <record>.)
# =====================================================================

# Explain payload whose elements all carry a namespace PREFIX (ns1:),
# as some real SRU servers emit. Bare-name lookups miss these.
EXPLAIN_XML_PREFIXED = """\
<?xml version="1.0"?>
<zs:explainResponse xmlns:zs="http://www.loc.gov/zing/srw/">
  <zs:version>1.2</zs:version>
  <zs:record>
    <zs:recordSchema>http://explain.z3950.org/dtd/2.0/</zs:recordSchema>
    <zs:recordData>
      <ns1:explain xmlns:ns1="http://explain.z3950.org/dtd/2.0/">
        <ns1:databaseInfo><ns1:title>Pitt Alma</ns1:title></ns1:databaseInfo>
        <ns1:indexInfo>
          <ns1:set identifier="info:srw/cql-context-set/1/alma" name="alma"/>
          <ns1:index id="title" sort="true"><ns1:title>Title</ns1:title>
            <ns1:map><ns1:name set="alma">title</ns1:name></ns1:map></ns1:index>
          <ns1:index id="creator"><ns1:title>Author</ns1:title>
            <ns1:map><ns1:name set="alma">creator</ns1:name></ns1:map></ns1:index>
        </ns1:indexInfo>
        <ns1:schemaInfo>
          <ns1:schema identifier="http://www.loc.gov/MARC21/slim" name="marcxml">
            <ns1:title>MARCXML</ns1:title></ns1:schema>
        </ns1:schemaInfo>
      </ns1:explain>
    </zs:recordData>
  </zs:record>
</zs:explainResponse>
"""

# Explain payload where the SRW namespace is the DEFAULT (no zs: prefix),
# so xmltodict keys the envelope as bare 'record'/'recordData' -- and
# force_list=("record",) wraps it in a list, which broke _first.
EXPLAIN_XML_DEFAULT_NS = """\
<?xml version="1.0"?>
<explainResponse xmlns="http://www.loc.gov/zing/srw/">
  <version>1.2</version>
  <record>
    <recordData>
      <explain xmlns="http://explain.z3950.org/dtd/2.0/">
        <databaseInfo><title>DNB</title></databaseInfo>
        <indexInfo>
          <set identifier="x" name="dc"/>
          <index><title>Title</title><map><name set="dc">title</name></map></index>
        </indexInfo>
      </explain>
    </recordData>
  </record>
</explainResponse>
"""


class TestExplainNamespaceVariants:
    @pytest.mark.asyncio
    async def test_explain_with_prefixed_payload(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text=EXPLAIN_XML_PREFIXED,
                                        headers={"content-type": "text/xml"})
        )
        root = await sru.explain("http://test.example/sru")
        info = sru.parse_explain(root)
        assert info["title"] == "Pitt Alma"
        assert len(info["indexes"]) == 2
        assert info["indexes"][0]["name"] == "title"
        assert info["indexes"][0]["set"] == "alma"
        assert info["indexes"][0]["sort"] is True
        assert info["indexes"][1]["sort"] is False
        assert len(info["schemas"]) == 1
        assert info["schemas"][0]["name"] == "marcxml"

    @pytest.mark.asyncio
    async def test_explain_with_default_namespace(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text=EXPLAIN_XML_DEFAULT_NS,
                                        headers={"content-type": "text/xml"})
        )
        root = await sru.explain("http://test.example/sru")
        info = sru.parse_explain(root)
        assert info["title"] == "DNB"
        assert len(info["indexes"]) == 1
        assert info["indexes"][0]["name"] == "title"


class TestFirstIsListSafe:
    def test_first_returns_none_on_list(self):
        # Regression: _first must not do a membership test against a list.
        assert sru._first(["a", "b"], "a") is None

    def test_unwrap_single_element_list(self):
        assert sru._unwrap([{"x": 1}]) == {"x": 1}
        assert sru._unwrap([1, 2]) == [1, 2]
        assert sru._unwrap({"x": 1}) == {"x": 1}

    def test_get_ns_matches_localname(self):
        d = {"zs:record": 1, "ns1:databaseInfo": 2}
        assert sru._get_ns(d, "record") == 1
        assert sru._get_ns(d, "databaseInfo") == 2
        assert sru._get_ns(d, "missing") is None
        assert sru._get_ns(["not", "a", "dict"], "record") is None


class TestBuildCQLSort:
    def test_alma_appends_default_relevance_sort(self):
        assert (sru.build_cql(index_set="alma", keyword="whale")
                == 'alma.all_for_ui = whale sortBy alma.rank/sort.descending')

    def test_dc_has_no_default_sort(self):
        assert sru.build_cql(index_set="dc", keyword="whale") == "cql.anywhere = whale"

    def test_explicit_sort_overrides_default(self):
        assert (sru.build_cql(index_set="alma", title="x", sort="alma.title/sort.ascending")
                == 'alma.title = x sortBy alma.title/sort.ascending')

    def test_empty_string_sort_disables(self):
        assert sru.build_cql(index_set="alma", keyword="whale", sort="") == "alma.all_for_ui = whale"


class TestSchemaResolvers:
    def test_dnb_default_schema(self):
        assert sru.server_default_schema("dnb") == "oai_dc"

    def test_pitt_default_schema(self):
        assert sru.server_default_schema("pitt") == "marcxml"

    def test_pitt_default_index(self):
        assert sru.server_default_index("pitt") == "alma"

    def test_loc_default_index(self):
        assert sru.server_default_index("loc") == "dc"

    def test_unknown_server_schema_fallback(self):
        assert sru.server_default_schema("http://unknown.example/sru") == "marcxml"

    def test_unknown_server_index_fallback(self):
        assert sru.server_default_index("http://unknown.example/sru") == "dc"


# =====================================================================
# Server-name fallback when explain omits databaseInfo/title
# (Alma institutions often leave the SRU profile title blank, so explain
# yields no title even though schemas/indexes parse. server.py falls back
# to the configured server name. This tests parse_explain's half: a
# databaseInfo-less doc parses with title "Unknown", schemas/indexes intact.)
# =====================================================================

EXPLAIN_XML_NO_DBINFO = """\
<?xml version="1.0"?>
<srw:explainResponse xmlns:srw="http://www.loc.gov/zing/srw/" xmlns:zr="http://explain.z3950.org/dtd/2.0/">
  <srw:version>1.2</srw:version>
  <srw:record>
    <srw:recordData>
      <zr:explain>
        <zr:serverInfo protocol="SRU"><host>pitt.alma.exlibrisgroup.com</host></zr:serverInfo>
        <zr:indexInfo>
          <set identifier="x" name="alma"/>
          <index sort="true"><title>Title</title><map><name set="alma">title</name></map></index>
        </zr:indexInfo>
        <zr:schemaInfo>
          <schema identifier="marc" name="marcxml"><title>MARCXML</title></schema>
        </zr:schemaInfo>
      </zr:explain>
    </srw:recordData>
  </srw:record>
</srw:explainResponse>
"""


class TestExplainMissingDatabaseInfo:
    @pytest.mark.asyncio
    async def test_explain_without_databaseinfo_still_parses(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text=EXPLAIN_XML_NO_DBINFO,
                                        headers={"content-type": "text/xml"})
        )
        root = await sru.explain("http://test.example/sru")
        info = sru.parse_explain(root)
        # No databaseInfo -> title is "Unknown" at the parse layer (server.py
        # then substitutes the configured server name).
        assert info["title"] == "Unknown"
        # ...but schemas and indexes still parse fine.
        assert len(info["schemas"]) == 1
        assert info["schemas"][0]["name"] == "marcxml"
        assert len(info["indexes"]) == 1
        assert info["indexes"][0]["name"] == "title"
        assert info["indexes"][0]["sort"] is True

    @pytest.mark.asyncio
    async def test_explain_canonical_databaseinfo_title(self, mock_sru_server):
        # Canonical ZeeRex: zr: prefix on databaseInfo, bare <title> with attrs.
        xml = EXPLAIN_XML_NO_DBINFO.replace(
            '<zr:serverInfo protocol="SRU"><host>pitt.alma.exlibrisgroup.com</host></zr:serverInfo>',
            '<zr:databaseInfo><title lang="en" primary="true">Pitt ULS</title></zr:databaseInfo>'
        )
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text=xml,
                                        headers={"content-type": "text/xml"})
        )
        root = await sru.explain("http://test.example/sru")
        info = sru.parse_explain(root)
        assert info["title"] == "Pitt ULS"


# =====================================================================
# DC records whose elements carry an inline xmlns (LoC-style).
# xmltodict represents <title xmlns="...">X</title> as a dict
# {'@xmlns': ..., '#text': 'X'}, not a bare string. Regression guard:
# these must flatten to clean text, not stringified dicts, and must
# render without list brackets (incl. language, a single-value field).
# =====================================================================

SEARCH_XML_DC_INLINE_NS = """\
<?xml version="1.0"?>
<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:numberOfRecords>470</srw:numberOfRecords>
  <srw:records>
    <srw:record>
      <srw:recordSchema>info:srw/schema/1/dc-v1.1</srw:recordSchema>
      <srw:recordData>
        <dc xmlns="info:srw/schema/1/dc-schema">
          <title xmlns="http://purl.org/dc/elements/1.1/">Moby Dick ; Moby Dick</title>
          <creator xmlns="http://purl.org/dc/elements/1.1/">Melville, Herman</creator>
          <language xmlns="http://purl.org/dc/elements/1.1/">eng</language>
          <identifier xmlns="http://purl.org/dc/elements/1.1/">123</identifier>
          <identifier xmlns="http://purl.org/dc/elements/1.1/">456</identifier>
        </dc>
      </srw:recordData>
    </srw:record>
  </srw:records>
</srw:searchRetrieveResponse>
"""


class TestDublinCoreInlineNamespace:
    @pytest.mark.asyncio
    async def test_dc_elements_with_inline_xmlns_flatten_to_text(self, mock_sru_server):
        mock_sru_server.get("http://test.example/sru").mock(
            return_value=httpx.Response(200, text=SEARCH_XML_DC_INLINE_NS,
                                        headers={"content-type": "text/xml"})
        )
        root = await sru.search_retrieve("http://test.example/sru", 'cql.anywhere = "moby dick"')
        res = sru.parse_search_results(root)
        r0 = res["records"][0]
        # Clean text, not stringified dicts:
        assert r0["title"] == ["Moby Dick ; Moby Dick"]
        assert r0["author"] == ["Melville, Herman"]
        assert r0["language"] == ["eng"]
        assert r0["identifier"] == ["123", "456"]
        assert "{" not in str(r0["title"])  # no dict leakage

    def test_text_values_helper(self):
        assert sru._text_values("plain") == ["plain"]
        assert sru._text_values({"@xmlns": "x", "#text": "wrapped"}) == ["wrapped"]
        assert sru._text_values([{"#text": "a"}, "b"]) == ["a", "b"]
        assert sru._text_values(None) == []
        assert sru._text_values({"@xmlns": "x"}) == []   # no text -> dropped

    def test_language_renders_without_brackets(self):
        # A DC-parsed record has language as a list; the formatter must not
        # print Python list syntax.
        md = sru.format_search_results_markdown(
            {"total": 1, "records": [{"title": ["T"], "language": ["eng"], "schema": "dc"}]}
        )
        assert "**Language:** eng" in md
        assert "['eng']" not in md


# =====================================================================
# Capability discovery + on-disk cache (self-configuring layer)
# Discovery validates/annotates curated config; never overrides it.
# All tests redirect the cache to a temp dir so the real ~/.sru-mcp is
# never touched.
# =====================================================================

import tempfile
from pathlib import Path as _Path


@pytest.fixture
def temp_cache(monkeypatch):
    """Redirect sru's on-disk cache to a throwaway temp dir."""
    d = _Path(tempfile.mkdtemp())
    monkeypatch.setattr(sru, "_CACHE_DIR", d)
    monkeypatch.setattr(sru, "_CACHE_FILE", d / "explain_cache.json")
    return d


_DISCOVERY_INFO = {
    "title": "University of Pittsburgh",
    "schemas": [{"name": "marcxml"}, {"name": "dc"}, {"name": "mods"}],
    "indexes": [
        {"set": "alma", "name": "title", "title": "Title", "sort": True},
        {"set": "alma", "name": "creator", "title": "Creator", "sort": True},
        {"set": "alma", "name": "isbn", "title": "ISBN", "sort": False},
        {"set": "alma", "name": "all_for_ui", "title": "Keyword", "sort": False},
    ],
}


def _seed_profile(server="pitt"):
    profile = sru._profile_from_explain(_DISCOVERY_INFO)
    profile["fetched_at"] = "2026-06-30T18:30:00Z"
    cache = sru._load_cache()
    cache["servers"][server] = profile
    sru._save_cache(cache)
    return profile


class TestCapabilityDiscovery:
    def test_undiscovered_returns_none(self, temp_cache):
        assert sru.get_capabilities("pitt") is None
        # None means "unknown", callers must not warn or fail on it.
        assert sru.index_exists("pitt", "alma.title") is None
        assert sru.index_is_sortable("pitt", "alma.title") is None

    def test_profile_distillation(self, temp_cache):
        p = sru._profile_from_explain(_DISCOVERY_INFO)
        assert p["title"] == "University of Pittsburgh"
        assert p["schemas"] == ["marcxml", "dc", "mods"]
        assert "alma.title" in p["indexes"]
        assert p["indexes"]["alma.title"]["sortable"] is True
        assert p["indexes"]["alma.isbn"]["sortable"] is False
        assert set(p["sortable"]) == {"alma.title", "alma.creator"}

    def test_cache_roundtrip(self, temp_cache):
        _seed_profile("pitt")
        assert sru._CACHE_FILE.exists()
        # Fresh read from disk preserves the profile.
        cache = sru._load_cache()
        assert "pitt" in cache["servers"]
        assert len(cache["servers"]["pitt"]["indexes"]) == 4

    def test_index_exists_after_discovery(self, temp_cache):
        _seed_profile("pitt")
        assert sru.index_exists("pitt", "alma.title") is True
        assert sru.index_exists("pitt", "alma.subjects") is False  # not in profile
        assert sru.index_is_sortable("pitt", "alma.title") is True
        assert sru.index_is_sortable("pitt", "alma.isbn") is False

    def test_corrupt_cache_tolerated(self, temp_cache):
        sru._CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        sru._CACHE_FILE.write_text("{ not valid json")
        assert sru._load_cache() == {"version": sru._CACHE_VERSION, "servers": {}}

    def test_wrong_version_cache_reset(self, temp_cache):
        sru._CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        sru._CACHE_FILE.write_text('{"version": 999, "servers": {"x": {}}}')
        # Unknown format -> start fresh rather than misread.
        assert sru._load_cache() == {"version": sru._CACHE_VERSION, "servers": {}}


class TestFieldsToIndexes:
    def test_maps_only_provided_fields(self):
        planned = sru.fields_to_indexes(
            "alma", {"title": "whale", "author": None, "subject": "x", "keyword": None}
        )
        assert planned == {"title": "alma.title", "subject": "alma.subjects"}

    def test_dc_index_set(self):
        planned = sru.fields_to_indexes("dc", {"title": "x", "keyword": "y"})
        assert planned == {"title": "dc.title", "keyword": "cql.anywhere"}

    def test_unknown_set_falls_back_to_dc(self):
        planned = sru.fields_to_indexes("nonsense", {"title": "x"})
        assert planned == {"title": "dc.title"}
