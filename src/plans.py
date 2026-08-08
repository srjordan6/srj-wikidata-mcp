"""Batch edit planning and application.

WHY A PLAN STEP EXISTS. Stephen chose batch approval over per-edit confirmation,
which means the approval has to happen against something he can actually read.
plan_edits turns a list of operations into a rendered plan — resolved property
and value labels, the reference attached to each statement, a warning list — and
stores it under an id. Nothing touches Wikidata until apply_plan is called with
that id and the confirm string. The two-step is the entire safety model.

WHY REFERENCES ARE MANDATORY. Both SRJ items currently carry zero references on
every statement, and with no sitelinks their notability rests on being supported
by serious publicly available sources. Adding more unsourced claims makes the
deletion case stronger, not weaker. So build_plan refuses any add-claim operation
without at least one reference, and says why.

Plans live in memory. A restart loses pending plans, which is the correct failure
mode: an unapproved plan should not survive a deploy and get applied later
against a changed item.
"""

import json
import logging
import time
import uuid
from typing import Any

from .wikidata import WikidataError, get_client

log = logging.getLogger("srj-wikidata.plans")

# Hard ceiling per plan. Not a technical limit — a self-imposed one, so a single
# approval can never turn into a mass edit.
MAX_OPS_PER_PLAN = 30

CONFIRM_STRING = "I-CONFIRM-WIKIDATA-EDIT"

# Properties that count as a reference. P248 stated in, P854 reference URL,
# P212 ISBN-13, P1476 title, P577 publication date, P813 retrieved.
REFERENCE_PROPERTIES = {"P248", "P854", "P143", "P4656", "P887"}

_PLANS: dict[str, dict] = {}


def _snak(pid: str, value: Any, datatype: str) -> dict:
    """Build a mainsnak/reference snak. Only the value types actually needed for
    a person, a company and four books are supported; anything else should fail
    loudly rather than be guessed at."""
    if datatype == "wikibase-item":
        dv = {"value": {"entity-type": "item", "id": value}, "type": "wikibase-entityid"}
    elif datatype == "string" or datatype == "external-id":
        dv = {"value": str(value), "type": "string"}
    elif datatype == "url":
        dv = {"value": str(value), "type": "string"}
    elif datatype == "time":
        # Expect an ISO date; precision 11 = day, 9 = year.
        v = str(value)
        precision = 11 if len(v) >= 10 else 9
        if precision == 9:
            v = f"{v[:4]}-00-00"
        dv = {
            "value": {
                "time": f"+{v}T00:00:00Z",
                "timezone": 0,
                "before": 0,
                "after": 0,
                "precision": precision,
                "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
            },
            "type": "time",
        }
    elif datatype == "monolingualtext":
        dv = {"value": {"text": str(value), "language": "en"}, "type": "monolingualtext"}
    elif datatype == "quantity":
        dv = {
            "value": {"amount": f"+{value}", "unit": "1"},
            "type": "quantity",
        }
    else:
        raise WikidataError(f"Unsupported datatype '{datatype}' for {pid}")
    return {"snaktype": "value", "property": pid, "datatype": datatype, "datavalue": dv}


def build_plan(
    operations: list[dict],
    rationale: str,
) -> dict:
    """Validate operations and store a plan. Touches nothing on Wikidata."""
    if not operations:
        raise WikidataError("No operations supplied.")
    if len(operations) > MAX_OPS_PER_PLAN:
        raise WikidataError(
            f"{len(operations)} operations exceeds the {MAX_OPS_PER_PLAN} per-plan "
            "ceiling. Split into smaller plans so each stays reviewable."
        )
    if not rationale or len(rationale.strip()) < 10:
        raise WikidataError(
            "A rationale is required — it becomes the record of why this batch was "
            "approved. Write a sentence."
        )

    client = get_client()
    warnings: list[str] = []
    rendered: list[dict] = []
    qids = sorted({op.get("qid", "") for op in operations if op.get("qid")})
    entities = client.get_entities(qids, props="labels|descriptions") if qids else {}

    for i, op in enumerate(operations, start=1):
        kind = op.get("op")
        qid = op.get("qid")
        # create_item is the one operation with no qid, because the item does
        # not exist yet. Everything else must name its target.
        if not qid and kind != "create_item":
            raise WikidataError(f"Operation {i}: 'qid' is required.")
        if kind not in {"set_label", "set_description", "add_claim", "add_reference", "remove_claim", "create_item"}:
            raise WikidataError(
                f"Operation {i}: unknown op '{kind}'. Allowed: set_label, "
                "set_description, add_claim, add_reference, remove_claim, create_item."
            )

        entity_label = (
            (entities.get(qid, {}).get("labels", {}).get("en", {}) or {}).get("value")
            or "(unknown item)"
        )
        row = {"n": i, "op": kind, "qid": qid, "item": entity_label}

        if kind in {"set_label", "set_description"}:
            if not op.get("value"):
                raise WikidataError(f"Operation {i}: 'value' is required for {kind}.")
            row["value"] = op["value"]
            row["language"] = op.get("language", "en")

        elif kind == "add_claim":
            pid = op.get("property")
            if not pid or not pid.startswith("P"):
                raise WikidataError(f"Operation {i}: 'property' (a P-id) is required.")
            if op.get("value") is None:
                raise WikidataError(f"Operation {i}: 'value' is required.")
            if not op.get("datatype"):
                raise WikidataError(
                    f"Operation {i}: 'datatype' is required — one of wikibase-item, "
                    "string, external-id, url, time, monolingualtext, quantity."
                )
            refs = op.get("references") or []
            if not refs:
                raise WikidataError(
                    f"Operation {i} ({pid} on {qid}): REFUSED — no references supplied. "
                    "Every statement this server adds must cite a source. Both SRJ "
                    "items already carry zero references on every statement, which is "
                    "their main deletion risk; adding more unsourced claims makes that "
                    "worse. Supply references as a list of "
                    "{property, value, datatype}, e.g. P854 reference URL or P248 "
                    "stated in."
                )
            bad = [r.get("property") for r in refs if r.get("property") not in REFERENCE_PROPERTIES]
            if bad:
                warnings.append(
                    f"Op {i}: reference properties {bad} are unusual for a reference "
                    "block; P854 (reference URL) or P248 (stated in) are the normal ones."
                )
            row["property"] = pid
            row["value"] = op["value"]
            row["datatype"] = op["datatype"]
            row["references"] = refs

        elif kind == "add_reference":
            if not op.get("claim_guid"):
                raise WikidataError(
                    f"Operation {i}: 'claim_guid' is required. Get it from get_entity."
                )
            refs = op.get("references") or []
            if not refs:
                raise WikidataError(
                    f"Operation {i}: 'references' is required and is the whole point "
                    "of this operation. Supply a list of {property, value, datatype}."
                )
            bad = [r.get("property") for r in refs if r.get("property") not in REFERENCE_PROPERTIES]
            if bad:
                warnings.append(
                    f"Op {i}: reference properties {bad} are unusual for a reference "
                    "block; P854 (reference URL) or P248 (stated in) are the normal ones."
                )
            row["claim_guid"] = op["claim_guid"]
            row["references"] = refs

        elif kind == "create_item":
            if not op.get("label"):
                raise WikidataError(f"Operation {i}: 'label' is required for create_item.")
            if not op.get("description"):
                raise WikidataError(
                    f"Operation {i}: 'description' is required for create_item. An item "
                    "with no description is indistinguishable from its neighbours in "
                    "search and is the first thing a patroller flags."
                )
            claims = op.get("claims") or []
            if not claims:
                raise WikidataError(
                    f"Operation {i}: 'claims' is required for create_item. A label and "
                    "description alone do not establish what the item is; at minimum give "
                    "P31 instance of."
                )
            for n, c in enumerate(claims, start=1):
                if not c.get("property") or not c.get("datatype") or c.get("value") is None:
                    raise WikidataError(
                        f"Operation {i}, claim {n}: property, value and datatype are all required."
                    )
            sourced = sum(1 for c in claims if c.get("references"))
            if sourced == 0:
                raise WikidataError(
                    f"Operation {i}: REFUSED \u2014 not one claim on this new item carries a "
                    "reference. A new item with no sourcing anywhere is the weakest possible "
                    "position if its notability is ever questioned, and the deletion "
                    "discussion attaches publicly to the subject. Source at least the "
                    "identifying claims."
                )
            row["label"] = op["label"]
            row["description"] = op["description"]
            row["claim_count"] = len(claims)
            row["sourced_claims"] = sourced
            row["claims"] = claims
            warnings.append(
                f"Op {i}: CREATES A NEW WIKIDATA ITEM. This cannot be undone through this "
                "server \u2014 deletion requires a community nomination. Confirm the subject "
                "meets Wikidata notability before approving."
            )
            if sourced < len(claims):
                warnings.append(
                    f"Op {i}: {len(claims) - sourced} of {len(claims)} claims are unsourced."
                )

        elif kind == "remove_claim":
            if not op.get("claim_guid"):
                raise WikidataError(
                    f"Operation {i}: 'claim_guid' is required. Get it from get_entity."
                )
            row["claim_guid"] = op["claim_guid"]
            warnings.append(
                f"Op {i}: removal is not reversible through this server — the revision "
                "history keeps it, but restoring means a manual undo on Wikidata."
            )

        rendered.append(row)

    plan_id = f"plan_{uuid.uuid4().hex[:10]}"
    plan = {
        "plan_id": plan_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rationale": rationale.strip(),
        "operation_count": len(rendered),
        "items_touched": qids,
        "operations": rendered,
        "raw": operations,
        "warnings": warnings,
        "status": "pending",
        "confirm_with": CONFIRM_STRING,
        "note": (
            "Nothing has been written to Wikidata. Review the operations, then call "
            f"apply_plan(plan_id='{plan_id}', confirm='{CONFIRM_STRING}')."
        ),
    }
    _PLANS[plan_id] = plan
    return {k: v for k, v in plan.items() if k != "raw"}


def list_plans() -> dict:
    return {
        "ok": True,
        "plans": [
            {
                "plan_id": p["plan_id"],
                "created_at": p["created_at"],
                "status": p["status"],
                "operation_count": p["operation_count"],
                "items_touched": p["items_touched"],
                "rationale": p["rationale"][:120],
            }
            for p in _PLANS.values()
        ],
    }


def apply_plan(plan_id: str, confirm: str) -> dict:
    plan = _PLANS.get(plan_id)
    if not plan:
        raise WikidataError(
            f"No plan '{plan_id}'. Plans are held in memory and are lost on restart — "
            "rebuild it with plan_edits."
        )
    if plan["status"] != "pending":
        raise WikidataError(f"Plan '{plan_id}' is already {plan['status']}.")
    if confirm != CONFIRM_STRING:
        raise WikidataError(
            f"confirm must equal '{CONFIRM_STRING}' exactly. This is the approval gate."
        )

    client = get_client()
    if not client.authenticated:
        raise WikidataError(
            "No WIKIDATA_OAUTH_TOKEN configured — this server cannot write. Set it in "
            "the Render dashboard."
        )

    results: list[dict] = []
    created: list[str] = []
    failed = 0
    for op in plan["raw"]:
        kind, qid = op["op"], op.get("qid")
        try:
            if kind == "set_label":
                client.set_label(qid, op["value"], op.get("language", "en"))
            elif kind == "set_description":
                client.set_description(qid, op["value"], op.get("language", "en"))
            elif kind == "add_claim":
                snak = _snak(op["property"], op["value"], op["datatype"])
                refsnaks: dict[str, list] = {}
                for r in op["references"]:
                    rs = _snak(r["property"], r["value"], r.get("datatype", "url"))
                    refsnaks.setdefault(r["property"], []).append(rs)
                payload = {
                    "claims": [
                        {
                            "mainsnak": snak,
                            "type": "statement",
                            "rank": "normal",
                            "references": [{"snaks": refsnaks}],
                        }
                    ]
                }
                client.edit_entity(qid, json.dumps(payload))
            elif kind == "add_reference":
                refsnaks: dict[str, list] = {}
                for r in op["references"]:
                    rs = _snak(r["property"], r["value"], r.get("datatype", "url"))
                    refsnaks.setdefault(r["property"], []).append(rs)
                client.set_reference(op["claim_guid"], json.dumps(refsnaks))
            elif kind == "create_item":
                payload: dict = {
                    "labels": {"en": {"language": "en", "value": op["label"]}},
                    "descriptions": {"en": {"language": "en", "value": op["description"]}},
                    "claims": [],
                }
                for c in op["claims"]:
                    stmt: dict = {
                        "mainsnak": _snak(c["property"], c["value"], c["datatype"]),
                        "type": "statement",
                        "rank": "normal",
                    }
                    if c.get("references"):
                        rs: dict[str, list] = {}
                        for r in c["references"]:
                            rs.setdefault(r["property"], []).append(
                                _snak(r["property"], r["value"], r.get("datatype", "url"))
                            )
                        stmt["references"] = [{"snaks": rs}]
                    payload["claims"].append(stmt)
                resp = client.create_entity(json.dumps(payload))
                new_qid = (resp.get("entity") or {}).get("id")
                if not new_qid:
                    raise WikidataError(
                        "Item creation returned no entity id; treating as failed rather "
                        "than assuming an item was created."
                    )
                created.append(new_qid)
                results.append({"op": kind, "qid": new_qid, "ok": True,
                                "created": new_qid,
                                "url": f"https://www.wikidata.org/wiki/{new_qid}"})
                continue
            elif kind == "remove_claim":
                client.remove_claims([op["claim_guid"]])
            results.append({"op": kind, "qid": qid, "ok": True})
        except Exception as e:  # noqa: BLE001 — one failure must not abort the rest
            failed += 1
            results.append({"op": kind, "qid": qid, "ok": False, "error": str(e)})
            log.warning("Plan %s op failed on %s: %s", plan_id, qid, e)

    plan["status"] = "applied" if failed == 0 else "partially_applied"
    plan["applied_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "ok": failed == 0,
        "plan_id": plan_id,
        "status": plan["status"],
        "applied": len(results) - failed,
        "failed": failed,
        "created_items": created,
        "results": results,
        "verify": [f"https://www.wikidata.org/wiki/{q}" for q in plan["items_touched"] + created],
    }
