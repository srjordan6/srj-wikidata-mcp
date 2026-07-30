# srj-wikidata-mcp

MCP server for maintaining the Wikidata items belonging to Stephen R. Jordan,
SRJ Consulting & Services LLC, and the published volumes of *The Operating
Discipline for AI Library*.

| Subject | Item |
|---|---|
| Stephen R. Jordan (person) | [Q140622333](https://www.wikidata.org/wiki/Q140622333) |
| SRJ Consulting & Services LLC | [Q140622666](https://www.wikidata.org/wiki/Q140622666) |
| Published volumes | separate items, linked from the person via `P800` notable work |

Built to mirror `srj-mcp`: FastMCP, OAuth 2.1 + PKCE for Claude.ai's custom
connector flow, Docker on Render, public `/healthz`. Deployed as a **separate
service** on purpose — a different credential and a different blast radius, so a
bug here cannot reach the audit database.

## Tools

**Reads** — unrestricted.

| Tool | Purpose |
|---|---|
| `health_check` | API reachable; reports whether writes are enabled and as which user |
| `search_entity` | Find Q-ids or P-ids by name |
| `get_entity` | Labels, descriptions, statements with claim GUIDs, **and a count of unsourced statements** |
| `sparql` | Read-only query against `query.wikidata.org` |

**Writes** — two-step, no single-claim write tool exists.

| Tool | Purpose |
|---|---|
| `plan_edits` | Validate and render a batch. Touches nothing. Returns a `plan_id` |
| `apply_plan` | Execute a plan. Requires `confirm="I-CONFIRM-WIKIDATA-EDIT"` |
| `list_plans` | Pending and applied plans in this process |

## Why writes are shaped this way

Wikidata's bot policy requires community approval before automated editing, and
an LLM writing statements via the API at volume **is** a bot whatever it is
called. This server is built for the other thing: a human approving a small,
sourced, reviewable batch that is attributable in the revision history.

Enforced, not advisory:

- **Every `add_claim` must carry at least one reference.** Refused otherwise.
  Both items currently have zero references on every statement, and with no
  sitelinks their notability rests on being supported by serious publicly
  available sources — so adding unsourced claims makes the deletion case
  *stronger*, not weaker.
- **30 operations per plan.** Split larger work.
- **2 seconds between writes**, `maxlag=5`, `bot=0`, and every edit summarised as
  `via srj-wikidata-mcp (assistant-prepared, human-approved batch)`.
- **Plans live in memory.** A restart discards pending plans, which is correct:
  an unapproved plan should not survive a deploy and get applied later against a
  changed item.

## Setup

### 1. Register a Wikimedia OAuth consumer

At [Special:OAuthConsumerRegistration](https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose/oauth2):

- Choose **OAuth 2.0**
- Tick **"This consumer is for use only by <your username>"** (owner-only — no
  third-party authorisation dance, and the token acts solely as you)
- Grants: **Edit existing pages**, and **Create, edit, and move pages**
- Copy the **access token** it issues

Leave `WIKIDATA_OAUTH_TOKEN` blank to run read-only. Every read tool still works.

### 2. Deploy

```bash
git init && git add -A && git commit -m "srj-wikidata-mcp initial"
git remote add origin <your remote>
git push -u origin main
```

Render → New → Blueprint → point at this repo. `render.yaml` provisions it.
Then set the two secrets in the dashboard (both are `sync: false`):

- `WIKIDATA_OAUTH_TOKEN`
- `WIKIDATA_MCP_TOKEN` — `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`

### 3. Verify

```bash
curl https://srj-wikidata-mcp.onrender.com/healthz
```

Then add `https://srj-wikidata-mcp.onrender.com/mcp` as a custom connector in
Claude settings and call `health_check`. If it returns `anonymous: true`, the
OAuth token is invalid, expired, or missing the edit grant.

### Local

```bash
cp .env.example .env    # fill in
pip install -r requirements.txt
uvicorn src.server:app --host 0.0.0.0 --port 8000
```

## Known defects in the live items, as at 2026-07-29

Recorded in `srj_audit.career_narratives` under slug `wikidata-items`.

1. **Every statement on both items has zero references.** Highest priority.
2. Person item: LinkedIn personal profile ID is `stepherjordan` — a typo for
   `stephenrjordan`. Currently a dead external link.
3. Person item: *The AI Efficiency & Process Optimization™* appears **twice**
   under notable work.
4. Person item: no employer statements — Citi, Intel, McAfee, Optiv all absent.
5. Company item: does not record the Nevada → Texas conversion of 22 May 2026.
6. Neither item has sitelinks on any Wikimedia project.

## Sourcing

Statements should be sourced from material that already exists and is checkable:

- `srj_audit.press_books` and `press_book_isbns` — titles, page counts, ISBNs,
  publication dates
- Open Library editions — `OL62330768M`, `OL62330769M`, `OL62330772M`,
  `OL62371435M`; author `OL16525496A`
- OpenCorporates `us_tx/0806615653` — Texas registration
- Library of Congress and Dewey classifications on Volumes I and III

Use `P854` (reference URL) or `P248` (stated in). Note that
`press_books.published_on` and Open Library disagree on Volumes I and III —
resolve that before citing either as a publication date.
