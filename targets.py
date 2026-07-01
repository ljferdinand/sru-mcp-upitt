"""Identity-discovery layer for the SRU MCP server.

Lets a user register the SRU endpoint for their own library without hand-editing
servers.json. Two jobs live here:

  1. A platform-template registry. Each known ILS platform declares how its SRU
     base URL is formed and what inputs it needs. Three kinds:
       - parametric : a fixed URL shape built from named parts (Alma).
       - host_based : {scheme}://{host}:{port}/{path} with platform defaults the
                      user can override (Koha, FOLIO).
       - direct     : no template; the user supplies a full SRU base URL ("other").
  2. Persistence for user-added servers in ~/.sru-mcp/user_servers.json, parallel
     to the explain cache and outside the repo, so runtime state does not sit next
     to versioned code.

This module is deliberately pure: it imports only json/pathlib/re and never
touches httpx or xmltodict. The network probe (explain) and the capability cache
live in sru.py; server.py orchestrates the two. Keeping this pure keeps it unit
-testable without the protocol dependencies installed.

Precedence for server resolution is enforced in sru.py: servers.json >
user_servers.json > discovered > hardcoded. servers.json always wins.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Platform template registry
# ---------------------------------------------------------------------------
#
# Each entry:
#   kind            : "parametric" | "host_based" | "direct"
#   url_template    : str with {named} slots, or None for direct
#   required_inputs : inputs that must be supplied
#   optional_inputs : {name: default} filled in when the user omits them
#   defaults        : the server-record fields baked at registration time
#                     (version, default_index, default_schema). A None
#                     default_schema means "resolve from the explain response".
#   description     : one-line help shown by sru_list_platforms.
#
# Adding a platform is a new row here, not a code change. Verify a new pattern
# against a live endpoint before adding it (an unverified template is an
# anchor-miss waiting to happen).

PLATFORM_TEMPLATES: dict[str, dict[str, Any]] = {
    "alma": {
        "kind": "parametric",
        "url_template": "https://{domain}/view/sru/{institution_code}",
        "required_inputs": ["domain", "institution_code"],
        "optional_inputs": {},
        "defaults": {"version": "1.2", "default_index": "alma", "default_schema": "marcxml"},
        "description": (
            "Ex Libris Alma. Inputs: domain (the Alma host in your browser address "
            "bar when logged in, e.g. pitt.alma.exlibrisgroup.com or "
            "eu03.alma.exlibrisgroup.com) and institution_code (e.g. 01PITT_INST). "
            "SRU must be enabled institution-side; if the probe fails, that is the "
            "first thing to check."
        ),
    },
    "koha": {
        "kind": "host_based",
        "url_template": "{scheme}://{host}:{port}/{database}",
        "required_inputs": ["host"],
        "optional_inputs": {"scheme": "http", "port": "9999", "database": "biblios"},
        "defaults": {"version": "1.1", "default_index": "dc", "default_schema": "marcxml"},
        "description": (
            "Koha ILS Zebra SRU server. Inputs: host (required); port (default "
            "9999) and database (default 'biblios') if non-standard; scheme "
            "(default http). Often internal-only; modern Koha may prefer its REST "
            "API, so a public probe may not answer."
        ),
    },
    "folio": {
        "kind": "host_based",
        "url_template": "{scheme}://{host}:{port}/{dbname}",
        "required_inputs": ["host", "dbname"],
        "optional_inputs": {"scheme": "http", "port": "9997"},
        "defaults": {"version": "1.1", "default_index": "dc", "default_schema": "marcxml"},
        "description": (
            "FOLIO via the mod-z3950 / Net::Z3950::FOLIO YAZ gateway. Inputs: host "
            "and dbname (the FOLIO tenant); port (default 9997); scheme (default "
            "http). The gateway is a separately-deployed add-on and is often not "
            "exposed publicly."
        ),
    },
    "other": {
        "kind": "direct",
        "url_template": None,
        "required_inputs": ["base_url"],
        "optional_inputs": {},
        "defaults": {"version": "1.1", "default_index": "dc", "default_schema": None},
        "description": (
            "Any SRU server, by full base URL. Input: base_url (the SRU endpoint, "
            "no query string). Version defaults to 1.1 and the record schema is "
            "chosen from the server's explain response."
        ),
    },
}


def list_platforms() -> list[dict[str, Any]]:
    """Return a display-friendly list of platform templates."""
    out: list[dict[str, Any]] = []
    for name, t in PLATFORM_TEMPLATES.items():
        out.append({
            "platform": name,
            "kind": t["kind"],
            "required_inputs": list(t["required_inputs"]),
            "optional_inputs": dict(t["optional_inputs"]),
            "description": t["description"],
        })
    return out


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Lowercase, alphanumerics and hyphens only, runs collapsed, ends trimmed.

    e.g. "University of Pittsburgh" -> "university-of-pittsburgh",
         "01PITT_INST" -> "01pitt-inst"."""
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def resolve_key(
    name: str,
    explicit_key: str | None,
    existing_keys: list[str],
) -> tuple[str | None, str | None]:
    """Decide the registry key for a new server. Returns (key, error).

    An explicit key that collides is an error (do not silently rename what the
    user chose). A key derived from the name that collides is auto-suffixed
    (-2, -3, ...). Uniqueness is checked against all known keys, so a shipped
    servers.json key is never overwritten."""
    existing = set(existing_keys)
    if explicit_key:
        key = slugify(explicit_key)
        if not key:
            return None, "The provided key is empty after normalization; choose an alphanumeric key."
        if key in existing:
            return None, f"Key '{key}' is already in use. Choose a different key."
        return key, None
    base = slugify(name) or "server"
    if base not in existing:
        return base, None
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}", None


# ---------------------------------------------------------------------------
# URL assembly
# ---------------------------------------------------------------------------

def assemble_url(platform: str, inputs: dict[str, Any]) -> tuple[str, list[str]]:
    """Build the SRU base URL for a platform from user inputs.

    Returns (url, advisories). Advisories are non-fatal notes (e.g. a normalized
    institution code) surfaced to the user. Raises ValueError for an unknown
    platform or missing required inputs."""
    tmpl = PLATFORM_TEMPLATES.get(platform)
    if tmpl is None:
        raise ValueError(
            f"Unknown platform '{platform}'. Known platforms: {list(PLATFORM_TEMPLATES)}."
        )

    advisories: list[str] = []
    kind = tmpl["kind"]

    if kind == "direct":
        base = str(inputs.get("base_url") or "").strip()
        if not base:
            raise ValueError("The 'other' platform requires 'base_url' (a full SRU endpoint URL).")
        base = base.rstrip("?").rstrip("/")
        if not (base.startswith("http://") or base.startswith("https://")):
            raise ValueError("base_url must start with http:// or https://.")
        return base, advisories

    missing = [r for r in tmpl["required_inputs"]
               if not str(inputs.get(r) or "").strip()]
    if missing:
        raise ValueError(
            f"Platform '{platform}' requires: {', '.join(missing)}. "
            f"Optional (with defaults): {tmpl['optional_inputs'] or 'none'}."
        )

    # Start from optional-input defaults, then overlay non-empty user values.
    values: dict[str, str] = {k: str(v) for k, v in tmpl["optional_inputs"].items()}
    for k, v in inputs.items():
        if v is not None and str(v).strip() != "":
            values[k] = str(v).strip()

    if platform == "alma":
        # Institution code is conventionally uppercase and usually ends in _INST.
        code = values.get("institution_code", "")
        if code.upper() != code:
            values["institution_code"] = code.upper()
            advisories.append(f"Institution code normalized to uppercase: {values['institution_code']}.")
            code = values["institution_code"]
        if not (code.endswith("_INST") or "_NETWORK" in code):
            advisories.append(
                "Institution code usually ends in '_INST' (e.g. 01PITT_INST). "
                "Double-check it if the probe fails."
            )
        # Domain should be a bare host, not a full URL.
        dom = values.get("domain", "")
        if "://" in dom:
            dom = dom.split("://", 1)[1]
            advisories.append("Stripped the scheme from the Alma domain; supply just the host.")
        values["domain"] = dom.strip("/")

    try:
        url = tmpl["url_template"].format(**values)
    except KeyError as exc:
        raise ValueError(f"Missing value for URL part {exc} on platform '{platform}'.")
    return url, advisories


def choose_default_schema(platform: str, advertised: list[str] | None) -> str:
    """Pick the record schema to bake into a constructed entry.

    Parametric/host_based platforms carry a known default. For the direct path
    the default is unset, so choose from what the server's explain advertised:
    prefer marcxml, else the first advertised schema, else oai_dc."""
    default = PLATFORM_TEMPLATES.get(platform, {}).get("defaults", {}).get("default_schema")
    if default:
        return default
    adv = [a for a in (advertised or []) if a]
    for a in adv:
        if a.lower() == "marcxml":
            return a
    if adv:
        return adv[0]
    return "oai_dc"


def build_entry(
    key: str,
    name: str,
    platform: str,
    url: str,
    inputs: dict[str, Any],
    default_schema: str,
    notes: str = "",
) -> dict[str, Any]:
    """Assemble a user_servers.json record.

    Functional core (read by sru.get_server and the resolvers): id, name, url,
    version, default_index, default_schema. Provenance (platform, inputs) is
    stored for a future re-probe/refresh and for debugging."""
    d = PLATFORM_TEMPLATES.get(platform, {}).get("defaults", {})
    return {
        "id": key,
        "name": name or key,
        "url": url,
        "version": d.get("version") or "1.1",
        "default_index": d.get("default_index") or "dc",
        "default_schema": default_schema,
        "notes": notes or f"Added via sru_add_target (platform: {platform}).",
        "platform": platform,
        "inputs": {k: v for k, v in inputs.items() if v not in (None, "")},
    }


# ---------------------------------------------------------------------------
# Persistence  (~/.sru-mcp/user_servers.json)
# ---------------------------------------------------------------------------

_USER_DIR = Path.home() / ".sru-mcp"
_USER_FILE = _USER_DIR / "user_servers.json"


def load_user_servers() -> list[dict[str, Any]]:
    """Read user-added servers, tolerant of a missing or corrupt file.

    Only well-formed entries (dicts with id and url) are returned, so a partial
    hand-edit can never crash resolution."""
    try:
        data = json.loads(_USER_FILE.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("id") and d.get("url")]


def save_user_servers(servers: list[dict[str, Any]]) -> bool:
    """Write the user-servers file, creating the directory. Never raises;
    persistence is best-effort and must not break a registration in memory."""
    try:
        _USER_DIR.mkdir(parents=True, exist_ok=True)
        _USER_FILE.write_text(json.dumps(servers, indent=2, sort_keys=True))
        return True
    except OSError:
        return False


def register_user_server(entry: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Add (or replace, by id) a server in the user-servers file. Returns
    (saved_ok, full_list). Replacing by id lets a re-registration update a
    previous one rather than duplicate it."""
    servers = [s for s in load_user_servers() if s.get("id") != entry["id"]]
    servers.append(entry)
    ok = save_user_servers(servers)
    return ok, servers


def remove_user_server(key: str) -> tuple[bool, list[dict[str, Any]]]:
    """Remove a user-added server by id. Returns (removed, remaining).

    removed is False when no user server had that id (so the caller can tell a
    real removal from a no-op). Only user_servers.json is touched; shipped
    servers live elsewhere and are never affected."""
    before = load_user_servers()
    remaining = [s for s in before if s.get("id") != key]
    removed = len(remaining) != len(before)
    if removed:
        save_user_servers(remaining)
    return removed, remaining
