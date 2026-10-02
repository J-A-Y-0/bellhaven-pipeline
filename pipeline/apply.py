"""Write approved proposals back to the CRM. Nothing here runs without a human approval."""
import json
from . import store
from .matcher import norm_street


class SOPViolation(Exception): ...
class StaleProposal(Exception): ...


def _guard(live: dict, fields: dict, expect: dict):
    # 1. Stale check: the account must still look the way it did when the proposal was made.
    for k, v in (expect or {}).items():
        if (live.get(k) or "") != (v or ""):
            raise StaleProposal(f"{live['account_id']}: {k} is now '{live.get(k)}', proposal assumed '{v}'. Re-run pipeline.")
    # 2. Billing SOP, enforced independently of the matcher: never re-parent an account
    #    that has revenue history AND outstanding AR.
    if "parent_id" in fields and (live.get("parent_id") or "") != fields["parent_id"]:
        if (live.get("lifetime_revenue") or 0) > 0 and (live.get("outstanding_ar") or 0) > 0:
            raise SOPViolation(f"{live['account_id']} has revenue ${live['lifetime_revenue']:,} and AR "
                               f"${live['outstanding_ar']:,}: parent change forbidden, use CHOW.")


def _resolve(fields: dict, created: dict) -> dict:
    """Replace '$created:N' placeholders with ids of accounts created earlier in the same proposal."""
    return {k: (created.get(v.split(":")[1], v) if isinstance(v, str) and v.startswith("$created:") else v)
            for k, v in fields.items()}


def _preflight(crm, p: dict, created: dict):
    """Validate the whole proposal against the live CRM before the first write, so a stale proposal
    can't leave half its changes behind (e.g. a CHOW account created, then the link refused)."""
    for act in p["actions"]:
        if act["op"] != "update":
            continue
        live = crm.get(act["account_id"])
        live = live.get("data", live)
        fields = _resolve(act["fields"], created)
        if not all((live.get(k) or "") == (v or "") for k, v in fields.items()):
            _guard(live, fields, act.get("expect"))
    pending = [a["fields"] for i, a in enumerate(p["actions"]) if a["op"] == "create" and str(i) not in created]
    if pending:  # someone may have created the account by hand since the proposal was made
        involved = {a["account_id"] for a in p["actions"] if a["op"] == "update"}
        live_all = [x for x in crm.all_accounts() if x["account_id"] not in involved and x["status"] != "Inactive"
                    and not x.get("duplicate_of_account") and not x.get("chow_current_account")]
        for f in pending:
            for x in live_all:
                if (x["billing_state"] == f["billing_state"] and x["billing_city"].lower() == f["billing_city"].lower()
                        and norm_street(x["billing_street"]) == norm_street(f["billing_street"])):
                    raise StaleProposal(f"{x['name']} ({x['account_id']}) already exists at {f['billing_street']}. Re-run pipeline.")


def apply_proposal(crm, p: dict) -> dict:
    """Idempotent per action: ids of already-created accounts are kept in result so a retry after a
    partial failure never creates the same account twice."""
    result = p["result"] = p.get("result") or {"created": {}, "updated": []}  # attached to p: survives a failure
    created = result.setdefault("created", {})
    _preflight(crm, p, created)
    for i, act in enumerate(p["actions"]):
        fields = _resolve(act["fields"], created)
        if act["op"] == "create":
            if str(i) in created:
                continue
            resp = crm.create(fields)
            acct = resp.get("data", resp) if isinstance(resp, dict) else resp
            created[str(i)] = acct["account_id"]
        else:
            live = crm.get(act["account_id"])
            live = live.get("data", live)
            if all((live.get(k) or "") == (v or "") for k, v in fields.items()):
                continue  # already applied
            _guard(live, fields, act.get("expect"))
            crm.update(act["account_id"], fields)
            result.setdefault("updated", []).append(act["account_id"])
    return result


def decide(db, crm, fingerprint: str, decision: str, who="reviewer", note="") -> dict:
    row = db.execute("SELECT * FROM proposals WHERE fingerprint=?", (fingerprint,)).fetchone()
    if not row:
        raise KeyError(fingerprint)
    p = store.row_to_dict(row)
    if p["status"] not in ("pending", "failed"):
        raise ValueError(f"proposal is already {p['status']}")
    if decision == "reject":
        db.execute("UPDATE proposals SET status='rejected',decided_at=?,decided_by=?,reviewer_note=? WHERE fingerprint=?",
                   (store.now(), who, note, fingerprint))
        db.commit()
        return {"status": "rejected"}
    try:
        result = apply_proposal(crm, p)
        db.execute("UPDATE proposals SET status='applied',decided_at=?,decided_by=?,reviewer_note=?,result=?,error=NULL WHERE fingerprint=?",
                   (store.now(), who, note, json.dumps(result), fingerprint))
        db.commit()
        return {"status": "applied", "result": result}
    except Exception as e:  # p["result"] holds partial progress (created ids), so a retry is safe
        db.execute("UPDATE proposals SET status='failed',decided_at=?,decided_by=?,error=?,result=? WHERE fingerprint=?",
                   (store.now(), who, f"{type(e).__name__}: {e}", json.dumps(p.get("result")), fingerprint))
        db.commit()
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"}
