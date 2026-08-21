# srj-wikidata-mcp

A Model Context Protocol server for editing Wikidata, built around one
constraint: **an assistant cannot write a statement on its own.**

It exists because maintaining an item properly is slow, repetitive work — a
bibliographic record needs a title, a publication date, a page count, an ISBN
and an identifier crosswalk, each one sourced — and that is exactly the kind of
work an assistant should do and exactly the kind of work nobody should let an
assistant do unsupervised. So it drafts and a human approves.

Reads are unrestricted. Writes are two-step — a plan is drafted, rendered for
review, and applied only on explicit confirmation. There is no single-claim
write tool, and there is no way to add an unsourced claim.

FastMCP, OAuth 2.1 + PKCE for Claude.ai's custom connector flow, Docker on
Render, public `/healthz`.

---

## Why it is shaped this way

Wikidata's bot policy requires community approval before automated editing, and
an LLM writing statements through the API at volume **is** a bot whatever it is
called. This server is built for the other thing: a human approving a small,
sourced, reviewable batch that is attributable in the revision history.

The constraints below are enforced in code, not documented as guidance.

| Constraint | Effect |
|---|---|
| Every `add_claim` carries at least one reference | Unsourced claims are refused outright |
| 30 operations per plan | Large edits must be split and reviewed in pieces |
| Two seconds between writes, `maxlag=5`, `bot=0` | Respects the API under load rather than racing it |
| Every edit summarised as assistant-prepared, human-approved | The revision history says what actually happened |
| Plans live in memory only | A restart discards pending plans — an unapproved plan should not survive a deploy and get applied later against a changed item |

`get_entity` returns a count of **unsourced statements** alongside the claims,
because the number that matters when maintaining an item is not how many
statements it has but how many of them a reader can check.

### Writes report failure honestly

An early version reported success whenever the API call did not raise. It did
not check whether the response actually confirmed the edit, so a rejected write
was indistinguishable from an accepted one. Every write path now confirms a
positive success marker in the response before reporting success, and
`apply_plan` returns per-operation results with the created or modified Q-id so
the caller can verify independently rather than trusting the return value.

This matters more than it sounds. An assistant that cannot tell a failed write
from a successful one will confidently report work it never did.

---

## Tools

**Reads** — unrestricted.

| Tool | Purpose |
|---|---|
| `health_check` | API reachable; reports whether writes are enabled and as which user |
| `search_entity` | Find Q-ids or P-ids by name |
| `get_entity` | Labels, descriptions, statements with claim GUIDs, and a count of unsourced statements |
| `sparql` | Read-only query against `query.wikidata.org` |

**Writes** — two-step.

| Tool | Purpose |
|---|---|
| `plan_edits` | Validate and render a batch. Touches nothing. Returns a `plan_id` |
| `apply_plan` | Execute a plan. Requires `confirm="I-CONFIRM-WIKIDATA-EDIT"` |
| `list_plans` | Pending and applied plans in this process |

Supported operations: `create_item`, `set_label`, `set_description`,
`add_claim`, `add_reference`, `remove_claim`.

---

## Setup

### 1. Register a Wikimedia OAuth consumer

At [Special:OAuthConsumerRegistration](https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose/oauth2):

- Choose **OAuth 2.0**
- Tick **"This consumer is for use only by `<your username>`"** — owner-only, so
  there is no third-party authorisation dance and the token acts solely as you
- Grants: **Edit existing pages**, and **Create, edit, and move pages**
- Copy the **access token** it issues

Leave `WIKIDATA_OAUTH_TOKEN` blank to run read-only. Every read tool still works.

### 2. Deploy

Render → New → Blueprint → point at this repo. `render.yaml` provisions it.
Then set both secrets in the dashboard (both are `sync: false`):

- `WIKIDATA_OAUTH_TOKEN`
- `WIKIDATA_MCP_TOKEN` — `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`

### 3. Verify

```bash
curl https://your-service.onrender.com/healthz
```

Then add `https://your-service.onrender.com/mcp` as a custom connector in Claude
settings and call `health_check`. If it returns `anonymous: true`, the OAuth
token is invalid, expired, or missing the edit grant.

### Local

```bash
cp .env.example .env    # fill in
pip install -r requirements.txt
uvicorn src.server:app --host 0.0.0.0 --port 8000
```

---

## Sourcing statements

Statements should be sourced from material that already exists and is
independently checkable — a publisher record, a library catalogue, a company
registry — using `P854` (reference URL) or `P248` (stated in).

Where two sources disagree, resolve the disagreement before citing either.
Recording a date that a catalogue contradicts is worse than recording no date,
because it creates a citation that fails on inspection.

---

## Deployment note

This runs as a **separate service** from any other MCP server in the same
account, on a different credential. A bug here cannot reach anything else.

---

Built by [Stephen R. Jordan](https://github.com/srjordan6)
