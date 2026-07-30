"""SRJ Wikidata MCP Server — entry point.

Maintains the Wikidata items for Stephen R. Jordan (Q140622333), SRJ Consulting &
Services LLC (Q140622666) and the published volumes of The Operating Discipline
for AI Library.

SHAPE. Same as srj-mcp: FastMCP with OAuth 2.1 + PKCE for Claude.ai's custom
connector flow, Docker on Render, a public /healthz. Reads are unrestricted.
Writes go through a two-step plan/apply gate, never direct.

WHY PLAN/APPLY RATHER THAN DIRECT WRITE TOOLS. Wikidata's bot policy requires
community approval before automated editing, and an LLM writing statements via
the API at volume is a bot regardless of what it is called. This surface is built
for the other thing — a human approving a small, reviewable, sourced batch that
is attributable in the revision history. plan_edits renders; apply_plan writes,
and only against a confirm string. There is deliberately no tool that writes a
single claim on its own.

Run locally:
    WIKIDATA_OAUTH_TOKEN=... WIKIDATA_MCP_TOKEN=... \\
    uvicorn src.server:app --host 0.0.0.0 --port 8000
"""

# NOTE: no `from __future__ import annotations` — FastMCP introspects real types.

import logging
import os

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .auth_provider import WikidataOAuthProvider
from .plans import apply_plan as _apply_plan
from .plans import build_plan as _build_plan
from .plans import list_plans as _list_plans
from .wikidata import WikidataError, get_client

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("srj-wikidata")

ISSUER_URL = os.environ.get(
    "WIKIDATA_MCP_ISSUER_URL", "https://srj-wikidata-mcp.onrender.com"
)

# The items this server exists to maintain. Stated in the instructions so a
# session does not have to rediscover them.
SUBJECTS = {
    "person": "Q140622333",
    "company": "Q140622666",
}

mcp = FastMCP(
    name="srj_wikidata",
    instructions=(
        "Wikidata tool surface for Stephen R. Jordan's items. Person: Q140622333. "
        "Company (SRJ Consulting & Services LLC): Q140622666. Book items exist "
        "separately and are linked from the person item via notable work (P800).\n\n"
        "READS are free: search_entity, get_entity, sparql, health_check.\n\n"
        "WRITES are two-step and there is no single-claim write tool. Call "
        "plan_edits with a list of operations and a rationale; it validates and "
        "renders the batch without touching Wikidata. Show the plan to Stephen. "
        "Only after he approves, call apply_plan with the plan_id and "
        "confirm='I-CONFIRM-WIKIDATA-EDIT'.\n\n"
        "Every add_claim operation MUST carry at least one reference or it is "
        "refused. Both items currently have zero references on every statement, "
        "and with no sitelinks their notability depends on being supported by "
        "serious publicly available sources — so adding unsourced claims actively "
        "harms them. Source from the srj_audit database (press_books, "
        "press_book_isbns, career_*), Open Library editions, or the Texas business "
        "registry.\n\n"
        "Ceiling is 30 operations per plan and writes are spaced 2s apart. Do not "
        "attempt to work around either: Wikidata's bot policy requires community "
        "approval for automated editing, and this server is deliberately built to "
        "stay inside human-approved, low-volume, attributable editing."
    ),
    streamable_http_path="/mcp",
    auth_server_provider=WikidataOAuthProvider(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(ISSUER_URL),
        resource_server_url=AnyHttpUrl(ISSUER_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=["mcp"],
    ),
)


def _err(e: Exception) -> dict:
    return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
def health_check() -> dict:
    """Verify the Wikidata API is reachable and report whether writes are possible.

    Call this first in a session. Reports the authenticated username and rights
    when a token is configured, or write_enabled=false when it is not.
    """
    try:
        c = get_client()
        out: dict = {
            "ok": True,
            "api": os.environ.get("WIKIDATA_API", "https://www.wikidata.org/w/api.php"),
            "write_enabled": c.authenticated,
            "subjects": SUBJECTS,
        }
        if c.authenticated:
            who = c.whoami()
            out["user"] = who.get("name")
            out["anonymous"] = bool(who.get("anon"))
            out["groups"] = who.get("groups", [])
            if out["anonymous"]:
                out["ok"] = False
                out["error"] = (
                    "Token is present but Wikidata treats the request as anonymous — "
                    "the OAuth token is invalid, expired, or lacks the editing grant."
                )
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def search_entity(query: str, kind: str = "item", limit: int = 10) -> dict:
    """Search Wikidata for items or properties by name.

    Args:
        query: Text to search for, e.g. "author" or "SRJ Consulting".
        kind: "item" for Q-ids, "property" for P-ids.
        limit: Max results, capped at 50.
    """
    try:
        return {"ok": True, "results": get_client().search(query, kind, limit)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def get_entity(qid: str, include_claims: bool = True) -> dict:
    """Fetch an entity with its labels, descriptions, statements and sitelinks.

    Returns each statement's claim GUID, which is what remove_claim operations
    need. Also counts references per statement, so unsourced claims are visible.

    Args:
        qid: Entity id, e.g. "Q140622333".
        include_claims: Set false for just labels and descriptions.
    """
    try:
        props = "labels|descriptions|claims|sitelinks" if include_claims else "labels|descriptions"
        ents = get_client().get_entities([qid], props=props)
        e = ents.get(qid)
        if not e or "missing" in e:
            return {"ok": False, "error": f"{qid} does not exist."}
        out: dict = {
            "ok": True,
            "id": qid,
            "label": (e.get("labels", {}).get("en", {}) or {}).get("value"),
            "description": (e.get("descriptions", {}).get("en", {}) or {}).get("value"),
            "sitelink_count": len(e.get("sitelinks", {}) or {}),
            "url": f"https://www.wikidata.org/wiki/{qid}",
        }
        if include_claims:
            statements = []
            unsourced = 0
            for pid, claims in (e.get("claims") or {}).items():
                for c in claims:
                    nrefs = len(c.get("references") or [])
                    if nrefs == 0:
                        unsourced += 1
                    ms = c.get("mainsnak", {})
                    dv = (ms.get("datavalue") or {}).get("value")
                    if isinstance(dv, dict):
                        dv = dv.get("id") or dv.get("time") or dv.get("text") or dv
                    statements.append(
                        {
                            "property": pid,
                            "value": dv,
                            "datatype": ms.get("datatype"),
                            "claim_guid": c.get("id"),
                            "references": nrefs,
                            "rank": c.get("rank"),
                        }
                    )
            out["statements"] = statements
            out["statement_count"] = len(statements)
            out["unsourced_statement_count"] = unsourced
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def sparql(query: str, limit_rows: int = 500) -> dict:
    """Run a read-only SPARQL query against query.wikidata.org.

    Args:
        query: SPARQL. Always include a LIMIT; the service times out at 60s.
        limit_rows: Max rows returned to the caller, default 500.
    """
    try:
        return {"ok": True, **get_client().sparql(query, limit_rows)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# Write tools — plan then apply, never direct
# ---------------------------------------------------------------------------


@mcp.tool()
def plan_edits(operations: list, rationale: str) -> dict:
    """Validate and render a batch of Wikidata edits WITHOUT applying them.

    This is the only way to write through this server. It resolves item labels,
    checks every operation, and returns a reviewable plan plus a plan_id. Nothing
    reaches Wikidata until apply_plan is called.

    Each operation is a dict:
      {"op": "set_label",       "qid": "Q...", "value": "...", "language": "en"}
      {"op": "set_description", "qid": "Q...", "value": "...", "language": "en"}
      {"op": "add_claim",       "qid": "Q...", "property": "P...",
       "value": <see datatype>, "datatype": "wikibase-item|string|external-id|url|
       time|monolingualtext|quantity",
       "references": [{"property": "P854", "value": "https://...", "datatype": "url"}]}
      {"op": "remove_claim",    "qid": "Q...", "claim_guid": "Q...$GUID"}

    add_claim is REFUSED without at least one reference. Ceiling is 30 operations
    per plan.

    Args:
        operations: List of operation dicts as above.
        rationale: One or more sentences on why this batch is being made. Becomes
            the approval record; must be at least 10 characters.
    """
    try:
        return {"ok": True, **_build_plan(operations, rationale)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def apply_plan(plan_id: str, confirm: str) -> dict:
    """Apply a plan from plan_edits to Wikidata. Requires explicit confirmation.

    Only call this after Stephen has seen the plan and approved it. Each operation
    is attempted independently, so one failure does not abort the rest; the result
    reports per-operation outcomes and links to verify each item.

    Args:
        plan_id: The plan_id returned by plan_edits.
        confirm: Must equal "I-CONFIRM-WIKIDATA-EDIT" exactly.
    """
    try:
        return _apply_plan(plan_id, confirm)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def list_plans() -> dict:
    """List plans held in this process, with status. Plans are lost on restart."""
    try:
        return _list_plans()
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# Public HTTP routes
# ---------------------------------------------------------------------------


async def healthz(request):
    """Unauthenticated probe. Does not call Wikidata — Render restarts on 503 and
    an upstream blip should not cycle the service."""
    return JSONResponse({"ok": True, "service": "srj-wikidata-mcp"})


async def landing(request):
    return HTMLResponse(
        """<!doctype html><html><head><title>SRJ Wikidata MCP</title>
        <style>body{font-family:system-ui,sans-serif;max-width:540px;margin:4em auto;
        padding:0 1em;color:#222}code{background:#f4f4f4;padding:0 .25em;border-radius:3px}
        a{color:#14467f}</style></head><body>
        <h1>SRJ Wikidata MCP Server</h1>
        <p>OAuth 2.1 + PKCE protected MCP transport at <code>/mcp</code>.</p>
        <p>Maintains Wikidata items Q140622333 (person) and Q140622666 (company).
        Writes require a reviewed plan and an explicit confirmation string.</p>
        <p>Discovery: <a href="/.well-known/oauth-authorization-server">
        /.well-known/oauth-authorization-server</a> &middot;
        Health: <a href="/healthz">/healthz</a></p>
        </body></html>"""
    )


app = mcp.streamable_http_app()
app.routes.append(Route("/", landing))
app.routes.append(Route("/healthz", healthz))
