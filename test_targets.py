"""Offline tests for targets.py. Pure logic, no network, no httpx/xmltodict.

Run: python test_targets.py
"""

import sys
import tempfile
from pathlib import Path

import targets


passed = 0
failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {label}\n    got:  {got!r}\n    want: {want!r}")


def check_true(label, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {label} (expected true)")


def check_raises(label, fn):
    global passed, failed
    try:
        fn()
    except ValueError:
        passed += 1
    except Exception as exc:  # noqa
        failed += 1
        print(f"  FAIL {label} (raised {type(exc).__name__}, wanted ValueError)")
    else:
        failed += 1
        print(f"  FAIL {label} (did not raise)")


# --- slugify -----------------------------------------------------------------
check("slugify spaces", targets.slugify("University of Pittsburgh"), "university-of-pittsburgh")
check("slugify code", targets.slugify("01PITT_INST"), "01pitt-inst")
check("slugify trim", targets.slugify("  --Weird__Name!!  "), "weird-name")
check("slugify empty", targets.slugify(""), "")


# --- resolve_key -------------------------------------------------------------
check("key from name", targets.resolve_key("My Library", None, ["pitt", "loc"]), ("my-library", None))
check("explicit ok", targets.resolve_key("x", "MyKey", ["pitt"]), ("mykey", None))
# explicit collision -> error
k, err = targets.resolve_key("x", "pitt", ["pitt", "loc"])
check("explicit collision key None", k, None)
check_true("explicit collision has error", err is not None)
# derived collision -> suffix
check("derived collision suffix", targets.resolve_key("Pitt", None, ["pitt"]), ("pitt-2", None))
check("derived collision suffix chain",
      targets.resolve_key("Pitt", None, ["pitt", "pitt-2", "pitt-3"]), ("pitt-4", None))
# empty explicit after slug -> error
k2, err2 = targets.resolve_key("x", "!!!", ["pitt"])
check("empty explicit None", k2, None)
check_true("empty explicit error", err2 is not None)
# never overwrites a shipped key (collision path triggers)
check("shipped protected", targets.resolve_key("LOC", None, ["loc"]), ("loc-2", None))


# --- assemble_url: alma (parametric) ----------------------------------------
url, adv = targets.assemble_url("alma", {"domain": "pitt.alma.exlibrisgroup.com", "institution_code": "01PITT_INST"})
check("alma url", url, "https://pitt.alma.exlibrisgroup.com/view/sru/01PITT_INST")
check("alma no advisories", adv, [])
# datacenter-form domain works identically (domain is an input, not derived)
url2, _ = targets.assemble_url("alma", {"domain": "eu03.alma.exlibrisgroup.com", "institution_code": "44GLA_INST"})
check("alma datacenter url", url2, "https://eu03.alma.exlibrisgroup.com/view/sru/44GLA_INST")
# lowercase code -> normalized + advisory
url3, adv3 = targets.assemble_url("alma", {"domain": "pitt.alma.exlibrisgroup.com", "institution_code": "01pitt_inst"})
check("alma code uppercased", url3, "https://pitt.alma.exlibrisgroup.com/view/sru/01PITT_INST")
check_true("alma uppercase advisory", any("uppercase" in a for a in adv3))
# scheme in domain -> stripped + advisory
url4, adv4 = targets.assemble_url("alma", {"domain": "https://pitt.alma.exlibrisgroup.com/", "institution_code": "01PITT_INST"})
check("alma domain stripped", url4, "https://pitt.alma.exlibrisgroup.com/view/sru/01PITT_INST")
check_true("alma strip advisory", any("scheme" in a for a in adv4))
# missing _INST -> advisory but still builds
url5, adv5 = targets.assemble_url("alma", {"domain": "x.alma.exlibrisgroup.com", "institution_code": "01PITT"})
check("alma builds without _INST", url5, "https://x.alma.exlibrisgroup.com/view/sru/01PITT")
check_true("alma _INST advisory", any("_INST" in a for a in adv5))
# missing required input -> ValueError
check_raises("alma missing code", lambda: targets.assemble_url("alma", {"domain": "x.alma.exlibrisgroup.com"}))


# --- assemble_url: koha / folio (host_based) --------------------------------
kurl, _ = targets.assemble_url("koha", {"host": "catalog.mylib.org"})
check("koha defaults", kurl, "http://catalog.mylib.org:9999/biblios")
kurl2, _ = targets.assemble_url("koha", {"host": "catalog.mylib.org", "port": "80", "database": "biblios", "scheme": "https"})
check("koha overrides", kurl2, "https://catalog.mylib.org:80/biblios")
furl, _ = targets.assemble_url("folio", {"host": "z.folio.org", "dbname": "TEST"})
check("folio defaults", furl, "http://z.folio.org:9997/TEST")
furl2, _ = targets.assemble_url("folio", {"host": "z.folio.org", "dbname": "TEST", "port": "210", "scheme": "https"})
check("folio overrides", furl2, "https://z.folio.org:210/TEST")
# folio requires dbname
check_raises("folio missing dbname", lambda: targets.assemble_url("folio", {"host": "z.folio.org"}))
# koha requires host
check_raises("koha missing host", lambda: targets.assemble_url("koha", {}))


# --- assemble_url: direct ("other") -----------------------------------------
durl, dadv = targets.assemble_url("other", {"base_url": "https://sru.k10plus.de/opac-de-627?"})
check("direct strips trailing ?", durl, "https://sru.k10plus.de/opac-de-627")
check("direct no advisories", dadv, [])
check_raises("direct missing base_url", lambda: targets.assemble_url("other", {}))
check_raises("direct bad scheme", lambda: targets.assemble_url("other", {"base_url": "ftp://x/y"}))
check_raises("unknown platform", lambda: targets.assemble_url("nope", {"base_url": "https://x"}))


# --- choose_default_schema ---------------------------------------------------
check("schema alma default", targets.choose_default_schema("alma", ["marcxml", "dc"]), "marcxml")
check("schema koha default", targets.choose_default_schema("koha", []), "marcxml")
# direct: prefer marcxml among advertised
check("schema direct prefers marcxml", targets.choose_default_schema("other", ["oai_dc", "marcxml", "mods"]), "marcxml")
# direct: else first advertised
check("schema direct first advertised", targets.choose_default_schema("other", ["mods", "dc"]), "mods")
# direct: none advertised -> oai_dc
check("schema direct fallback", targets.choose_default_schema("other", []), "oai_dc")


# --- build_entry -------------------------------------------------------------
entry = targets.build_entry(
    key="my-library",
    name="My Library",
    platform="alma",
    url="https://x.alma.exlibrisgroup.com/view/sru/01X_INST",
    inputs={"domain": "x.alma.exlibrisgroup.com", "institution_code": "01X_INST", "unused": ""},
    default_schema="marcxml",
)
check("entry id", entry["id"], "my-library")
check("entry url", entry["url"], "https://x.alma.exlibrisgroup.com/view/sru/01X_INST")
check("entry version from template", entry["version"], "1.2")
check("entry index from template", entry["default_index"], "alma")
check("entry schema", entry["default_schema"], "marcxml")
check("entry platform provenance", entry["platform"], "alma")
check("entry inputs drop empty", "unused" in entry["inputs"], False)
check_true("entry has notes", bool(entry["notes"]))
# direct entry uses generic defaults
dentry = targets.build_entry("g", "Generic", "other", "https://x/y", {"base_url": "https://x/y"}, "dc")
check("direct entry version", dentry["version"], "1.1")
check("direct entry index", dentry["default_index"], "dc")


# --- persistence (temp dir) --------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    targets._USER_DIR = Path(tmp)
    targets._USER_FILE = Path(tmp) / "user_servers.json"

    check("load empty when absent", targets.load_user_servers(), [])
    ok, servers = targets.register_user_server(entry)
    check_true("register saved", ok)
    check("one server after register", len(servers), 1)
    reloaded = targets.load_user_servers()
    check("reload finds it", len(reloaded), 1)
    check("reload id", reloaded[0]["id"], "my-library")
    # re-register same id replaces (no duplicate)
    entry_v2 = dict(entry, url="https://changed/view/sru/01X_INST")
    ok2, servers2 = targets.register_user_server(entry_v2)
    check("still one after re-register", len(servers2), 1)
    check("re-register updated url", targets.load_user_servers()[0]["url"], "https://changed/view/sru/01X_INST")
    # a second, different server
    ok3, servers3 = targets.register_user_server(targets.build_entry("g", "Generic", "other", "https://x/y", {}, "dc"))
    check("two servers now", len(servers3), 2)
    # remove one by id
    removed, remaining = targets.remove_user_server("g")
    check_true("remove returns removed=True", removed)
    check("one left after remove", len(remaining), 1)
    check("removed the right one", targets.load_user_servers()[0]["id"], "my-library")
    # removing a non-existent id is a no-op
    removed2, remaining2 = targets.remove_user_server("does-not-exist")
    check("remove non-existent removed=False", removed2, False)
    check("still one after no-op remove", len(remaining2), 1)
    # corrupt file -> empty, no crash
    targets._USER_FILE.write_text("{not json")
    check("corrupt file tolerated", targets.load_user_servers(), [])


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
