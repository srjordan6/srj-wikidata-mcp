"""Wikidata API client.

WHY THIS SHAPE. Wikidata is a MediaWiki install with the Wikibase extension, so
there are two APIs in play and they are not interchangeable. Entity statements
live behind the Action API's ``wb*`` modules (wbsearchentities, wbgetentities,
wbeditentity, wbsetreference); SPARQL lives on a separate host entirely
(query.wikidata.org). Both are wrapped here so no tool module has to know which
is which.

Writes are deliberately awkward. Every write goes through _post, which refuses
to run without a CSRF token, injects ``maxlag`` so we back off when the cluster
is behind, tags the edit so it is identifiable in the revision history, and
sleeps between calls. Wikidata's bot policy requires community approval before
automated editing; this client is built for approved-by-a-human, low-volume,
attributable edits, and the rate limiter is there to keep it honestly inside
that description rather than as a courtesy.

Auth is an OAuth 2.0 owner-only consumer access token in WIKIDATA_OAUTH_TOKEN.
Owner-only because the consumer acts solely as Stephen and never needs a
third-party authorisation dance.
"""

import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("srj-wikidata.client")

API = os.environ.get("WIKIDATA_API", "https://www.wikidata.org/w/api.php")
SPARQL = os.environ.get(
    "WIKIDATA_SPARQL", "https://query.wikidata.org/sparql"
)
# Wikimedia requires a descriptive, contactable User-Agent. A generic one gets
# rate-limited or blocked outright, and rightly so.
UA = os.environ.get(
    "WIKIDATA_USER_AGENT",
    "srj-wikidata-mcp/1.0 (https://srjconsultingservices.com; srj@srjconsultingservices.com)",
)
# Seconds between write calls. Not a throttle we are forced into — a self-imposed
# ceiling so this can never look like an unapproved bot run.
WRITE_INTERVAL = float(os.environ.get("WIKIDATA_WRITE_INTERVAL", "2.0"))
# maxlag tells the API to reject our write if replication is behind by more than
# this many seconds, rather than adding to the pile. 5 is the value Wikimedia
# asks automated clients to use.
MAXLAG = int(os.environ.get("WIKIDATA_MAXLAG", "5"))

EDIT_TAG_SUMMARY = "via srj-wikidata-mcp (assistant-prepared, human-approved batch)"


class WikidataError(RuntimeError):
    """Raised for any non-success response from the Wikidata APIs."""


class WikidataClient:
    def __init__(self, token: str | None = None):
        self._token = token or os.environ.get("WIKIDATA_OAUTH_TOKEN") or ""
        self._csrf: str | None = None
        self._last_write = 0.0

    # -- plumbing ----------------------------------------------------------

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict:
        h = {"User-Agent": UA}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _get(self, params: dict) -> dict:
        params = {**params, "format": "json", "formatversion": "2"}
        with httpx.Client(timeout=30) as c:
            r = c.get(API, params=params, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        if "error" in data:
            raise WikidataError(f"{data['error'].get('code')}: {data['error'].get('info')}")
        return data

    def _post(self, params: dict) -> dict:
        """Authenticated write. Handles CSRF, maxlag and the rate ceiling."""
        if not self._token:
            raise WikidataError(
                "No WIKIDATA_OAUTH_TOKEN configured — writes are unavailable. "
                "Read tools still work."
            )
        # Self-imposed spacing between writes.
        delta = time.time() - self._last_write
        if delta < WRITE_INTERVAL:
            time.sleep(WRITE_INTERVAL - delta)

        body = {
            **params,
            "format": "json",
            "formatversion": "2",
            "token": self.csrf_token(),
            "maxlag": MAXLAG,
            "errorformat": "plaintext",
        }
        with httpx.Client(timeout=60) as c:
            r = c.post(API, data=body, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        self._last_write = time.time()

        if "error" in data:
            err = data["error"]
            code = err.get("code")
            # A stale CSRF token is recoverable exactly once.
            if code == "badtoken":
                self._csrf = None
                raise WikidataError("badtoken: CSRF token was stale; retry the call")
            if code == "maxlag":
                raise WikidataError(
                    f"maxlag: Wikidata replication is behind ({err.get('info')}). "
                    "Wait and retry — do not loop."
                )
            raise WikidataError(f"{code}: {err.get('info') or err}")
        return data

    def csrf_token(self) -> str:
        if self._csrf:
            return self._csrf
        data = self._get({"action": "query", "meta": "tokens", "type": "csrf"})
        tok = data["query"]["tokens"]["csrftoken"]
        if tok == "+\\":
            raise WikidataError(
                "Wikidata returned an anonymous CSRF token — the OAuth token is "
                "missing, expired or lacks the editing grant."
            )
        self._csrf = tok
        return tok

    # -- reads -------------------------------------------------------------

    def whoami(self) -> dict:
        data = self._get({"action": "query", "meta": "userinfo", "uiprop": "rights|groups"})
        return data["query"]["userinfo"]

    def search(self, query: str, kind: str = "item", limit: int = 10, language: str = "en") -> list[dict]:
        data = self._get(
            {
                "action": "wbsearchentities",
                "search": query,
                "language": language,
                "uselang": language,
                "type": kind,
                "limit": min(limit, 50),
            }
        )
        return [
            {
                "id": e.get("id"),
                "label": e.get("label"),
                "description": e.get("description"),
                "match": (e.get("match") or {}).get("text"),
            }
            for e in data.get("search", [])
        ]

    def get_entities(self, ids: list[str], props: str = "labels|descriptions|claims|sitelinks") -> dict:
        if not ids:
            return {}
        data = self._get(
            {"action": "wbgetentities", "ids": "|".join(ids[:50]), "props": props, "languages": "en"}
        )
        return data.get("entities", {})

    def sparql(self, query: str, limit_rows: int = 500) -> dict:
        with httpx.Client(timeout=60) as c:
            r = c.get(
                SPARQL,
                params={"query": query},
                headers={"User-Agent": UA, "Accept": "application/sparql-results+json"},
            )
            r.raise_for_status()
            data = r.json()
        rows = data.get("results", {}).get("bindings", [])
        truncated = len(rows) > limit_rows
        return {
            "columns": data.get("head", {}).get("vars", []),
            "rows": [
                {k: v.get("value") for k, v in row.items()} for row in rows[:limit_rows]
            ],
            "row_count": min(len(rows), limit_rows),
            "truncated": truncated,
        }

    # -- writes ------------------------------------------------------------

    def set_label(self, qid: str, value: str, language: str = "en") -> dict:
        return self._post(
            {
                "action": "wbsetlabel",
                "id": qid,
                "language": language,
                "value": value,
                "summary": EDIT_TAG_SUMMARY,
                "bot": 0,
            }
        )

    def set_description(self, qid: str, value: str, language: str = "en") -> dict:
        return self._post(
            {
                "action": "wbsetdescription",
                "id": qid,
                "language": language,
                "value": value,
                "summary": EDIT_TAG_SUMMARY,
                "bot": 0,
            }
        )

    def edit_entity(self, qid: str, data_json: str, clear: bool = False) -> dict:
        """wbeditentity with a JSON payload. Used for claims plus references in
        one revision, so a sourced statement is never briefly unsourced."""
        params = {
            "action": "wbeditentity",
            "id": qid,
            "data": data_json,
            "summary": EDIT_TAG_SUMMARY,
            "bot": 0,
        }
        if clear:
            params["clear"] = 1
        return self._post(params)

    def remove_claims(self, claim_guids: list[str]) -> dict:
        return self._post(
            {
                "action": "wbremoveclaims",
                "claim": "|".join(claim_guids),
                "summary": EDIT_TAG_SUMMARY,
                "bot": 0,
            }
        )

    def set_reference(self, claim_guid: str, snaks_json: str) -> dict:
        """Attach a reference block to an EXISTING statement.

        This is the call that matters most for these items. wbeditentity can only
        add a reference as part of a statement it is also creating, so sourcing a
        claim that already exists needs wbsetreference against its GUID. Omitting
        ``reference`` means add-new rather than replace, so this is additive and
        will not clobber a reference someone else added.
        """
        return self._post(
            {
                "action": "wbsetreference",
                "statement": claim_guid,
                "snaks": snaks_json,
                "summary": EDIT_TAG_SUMMARY,
                "bot": 0,
            }
        )


_client: WikidataClient | None = None


def get_client() -> WikidataClient:
    global _client
    if _client is None:
        _client = WikidataClient()
    return _client
