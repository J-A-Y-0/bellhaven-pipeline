"""Matching + classification. Pure functions: (website rows, CRM rows) -> proposals.

Evidence hierarchy (most to least trustworthy):
  1. normalized street + city + state -> same physical facility (address is the identity).
     Resolved for every location first, so a weaker match can never take another location's account.
  2. same city+state, address differs: the website's administrator is a contact on the account,
     else name similarity >= NAME_THRESHOLD.
  Name alone never matches across cities: 'Bellhaven of Carlisle' (PA) vs 'Bellhaven of New Carlisle' (OH).
"""
import hashlib
import json
import os
import re
from collections import defaultdict
from rapidfuzz import fuzz

BELLHAVEN_ID = "0015QAPLGS3FVYEEEM"
NAME_THRESHOLD = int(os.getenv("NAME_THRESHOLD", 85))   # same-city name-only match (no address match)
REMOVED_NOTE_PREFIX = "Not listed on bellhaven website as of ownership sync"
COSMETIC_SIM = int(os.getenv("COSMETIC_SIM", 90))       # name differences at/above this similarity are labelled format-only

STREET_ABBR = {
    "avenue": "ave", "road": "rd", "boulevard": "blvd", "drive": "dr", "street": "st",
    "lane": "ln", "pike": "pk", "court": "ct", "place": "pl", "highway": "hwy",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}
CARE_MAP = {  # website wording -> CRM care_type vocabulary
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}


def norm_street(s: str) -> str:
    toks = re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()
    return " ".join(STREET_ABBR.get(t, t) for t in toks)


def norm_name(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\bthe\b", " ", s)
    s = s.replace("health care", "healthcare").replace("centre", "center")
    s = re.sub(r"\brehab\b", "rehabilitation", s)
    s = re.sub(r"\b(at|of)\b", " ", s)          # 'Bellhaven at X' vs 'Bellhaven of X'
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def name_sim(a: str, b: str) -> int:
    return round(fuzz.token_sort_ratio(norm_name(a), norm_name(b)))


def care_type(web_offerings: list[str]) -> str:
    for o in web_offerings:
        if o.lower() in CARE_MAP:
            return CARE_MAP[o.lower()]
    return ""


def is_parent_account(a: dict) -> bool:
    return a["name"].endswith("(Parent Account)")


def fingerprint(kind: str, subject: str, target: dict) -> str:
    raw = json.dumps([kind, subject, target], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def sop(acct: dict, new_parent_id: str) -> dict:
    """Billing SOP: moving an account to a different parent is only allowed in place when
    it has no revenue history OR no outstanding AR. Otherwise: CHOW (new account + link)."""
    rev, ar = acct["lifetime_revenue"] or 0, acct["outstanding_ar"] or 0
    changes_parent = (acct["parent_id"] or "") != new_parent_id
    chow = changes_parent and rev > 0 and ar > 0
    if not changes_parent:
        rule = "No parent change needed - SOP not triggered."
    elif chow:
        rule = (f"Revenue ${rev:,} > 0 AND outstanding AR ${ar:,} > 0 -> billing must keep the OLD account. "
                "Create new account under correct parent; set chow_current_account on the old one.")
    else:
        why = "no revenue history" if rev <= 0 else "no outstanding AR"
        rule = f"Parent change allowed in place ({why}: revenue ${rev:,}, AR ${ar:,})."
    return {"revenue": rev, "outstanding_ar": ar, "changes_parent": changes_parent, "chow_required": chow, "rule": rule}


def _view(a: dict) -> dict:
    keys = ["account_id", "name", "parent_name", "billing_street", "billing_city", "billing_state",
            "billing_zip", "care_type", "status", "lifetime_revenue", "outstanding_ar"]
    return {k: a.get(k) for k in keys}


def _web_view(w: dict) -> dict:
    return {k: w.get(k) for k in ["name", "street", "city", "state", "zip", "care_offerings", "url", "source"]}


def _score(c: dict, w: dict, contacts: dict, names: dict) -> tuple:
    """Survivor ranking inside a duplicate cluster (higher wins)."""
    return (
        (c["lifetime_revenue"] or 0) > 0,                 # billing history must be preserved
        _admin_hit(w, c, names),                          # website administrator is a contact here -> same facility
        c["parent_id"] == BELLHAVEN_ID,                   # already where it belongs
        contacts.get(c["account_id"], 0),                 # richer record
        norm_name(c["name"]) == norm_name(w["name"]),     # already carries the current brand name
        c["billing_street"] == w["street"],
        c["status"] == "Active",
        c["account_id"],                                  # deterministic tie-break
    )


def _admin_hit(w: dict, c: dict, names: dict) -> bool:
    """Website administrator appears among the CRM account's contacts. Supporting evidence only:
    a hit confirms identity; a miss proves nothing (staff turnover)."""
    adm = (w.get("administrator") or "").strip().lower()
    return bool(adm) and adm in {n.strip().lower() for n in names.get(c["account_id"], [])}


def propose(web: list[dict], crm: list[dict], contacts: dict | None = None, names: dict | None = None) -> dict:
    """Returns {'proposals': [...], 'confident': [...], 'summary': {...}}."""
    contacts, names = contacts or {}, names or {}
    # pool: skip parent accounts and records that were already superseded/merged
    pool = [a for a in crm if not is_parent_account(a)
            and not a.get("chow_current_account") and not a.get("duplicate_of_account")]
    by_id = {a["account_id"]: a for a in crm}
    claimed: set[str] = set()
    proposals, confident = [], []

    def add(kind, subject, target, title, actions, evidence, severity):
        for a in actions:  # show reviewers the current value next to every proposed value
            if a["op"] == "update":
                cur = by_id[a["account_id"]]
                a["before"] = {k: cur.get(k, "") for k in a["fields"]}
                if "parent_id" in a["fields"]:
                    a["before"]["parent_id"] = cur.get("parent_name") or "(none)"
        proposals.append({
            "fingerprint": fingerprint(kind, subject, target), "kind": kind, "title": title,
            "actions": actions, "evidence": evidence, "severity": severity})

    web = sorted(web, key=lambda x: x["name"])
    by_address = {}  # pass 1: address matches for every location before any fallback runs
    for w in web:
        by_address[w["slug"]] = [c for c in pool if c["account_id"] not in claimed
                                 and c["billing_state"] == w["state"]
                                 and c["billing_city"].lower() == w["city"].lower()
                                 and norm_street(c["billing_street"]) == norm_street(w["street"])]
        claimed.update(c["account_id"] for c in by_address[w["slug"]])

    for w in web:  # pass 2: fallback for the rest, then classify
        w_street = norm_street(w["street"])
        cluster = by_address[w["slug"]]
        match_basis = "address"
        if not cluster:
            cands = [(name_sim(w["name"], c["name"]), c) for c in pool
                     if c["account_id"] not in claimed and c["billing_state"] == w["state"]
                     and c["billing_city"].lower() == w["city"].lower()]
            cands.sort(key=lambda t: (not _admin_hit(w, t[1], names), -t[0]))   # administrator hit outranks name
            if cands and _admin_hit(w, cands[0][1], names):
                cluster, match_basis = [cands[0][1]], "administrator contact + city (street and name differ)"
            elif cands and cands[0][0] >= NAME_THRESHOLD:
                cluster, match_basis = [cands[0][1]], "name+city (street differs)"
            else:
                near = [{"account": _view(c), "name_similarity": s, "street_match": False,
                         "administrator_is_crm_contact": _admin_hit(w, c, names),
                         "contacts_on_account": names.get(c["account_id"], [])}
                        for s, c in cands[:3]]
                twins = [_view(c) for c in pool if c["billing_city"].lower() != w["city"].lower()
                         and norm_name(c["name"]) == norm_name(w["name"])]
                twins += [_view(c) for c in pool if c["billing_city"].lower() != w["city"].lower()
                          and name_sim(c["name"], w["name"]) >= 95 and _view(c) not in twins]
                add("CREATE", w["slug"], {"slug": w["slug"]},
                    f"Create account: {w['name']} ({w['city']}, {w['state']})",
                    [{"op": "create", "fields": {
                        "name": w["name"], "parent_id": BELLHAVEN_ID, "status": "Active",
                        "care_type": care_type(w["care_offerings"]), "phone": w.get("phone", ""),
                        "billing_street": w["street"], "billing_city": w["city"],
                        "billing_state": w["state"], "billing_zip": w["zip"],
                        "note": f"Created by Bellhaven ownership sync from {w['url']}"}}],
                    {"web": _web_view(w), "basis": "No CRM account matches this street address in this city; no same-city account has a similar name, "
                     f"and website administrator '{w.get('administrator') or 'n/a'}' is not a contact on any same-city account.", "near_misses_rejected": near,
                     "same_name_in_other_cities_ignored": twins,
                     "why_it_matters": "Facility is listed by Bellhaven but invisible to sales - no account to work, no corporate link."},
                    "high")
                continue

        cluster.sort(key=lambda c: _score(c, w, contacts, names), reverse=True)
        s, losers = cluster[0], cluster[1:]
        survivor_why = []
        if len(cluster) > 1:
            if (s["lifetime_revenue"] or 0) > 0: survivor_why.append("has billing history")
            if _admin_hit(w, s, names): survivor_why.append(f"website administrator '{w['administrator']}' is a contact here")
            if s["parent_id"] == BELLHAVEN_ID: survivor_why.append("already under Bellhaven")
            if contacts.get(s["account_id"], 0): survivor_why.append(f"{contacts[s['account_id']]} active contact(s)")
            if norm_name(s["name"]) == norm_name(w["name"]): survivor_why.append("name matches website")
            if s["billing_street"] == w["street"]: survivor_why.append("street string identical to website")
            survivor_why.append("ties broken deterministically by account_id")
        claimed.update(c["account_id"] for c in cluster)

        # ---- survivor: what (if anything) is wrong? --------------------------------
        sim = name_sim(w["name"], s["name"])
        fields, reasons = {}, []
        # The website is the source of truth for the facility's name, so any difference is aligned.
        # Similarity only labels it: a rebrand (real outdated name) vs a format-only variant (low risk).
        cosmetic_name = s["name"] != w["name"] and sim >= COSMETIC_SIM
        if s["name"] != w["name"]:
            fields["name"] = w["name"]
            reasons.append(f"name {'format differs' if cosmetic_name else 'outdated/rebranded'} "
                           f"('{s['name']}' -> '{w['name']}', similarity {sim})")
        zip_diff = s["billing_zip"] != w["zip"]
        street_diff = norm_street(s["billing_street"]) != w_street
        if street_diff:
            fields["billing_street"] = w["street"]
            reasons.append(f"street differs ('{s['billing_street']}' -> '{w['street']}')")
        if zip_diff:
            fields["billing_zip"] = w["zip"]
            reasons.append(f"ZIP differs ({s['billing_zip']} -> {w['zip']})")
        if s["status"] != "Active":
            fields["status"] = "Active"
            reasons.append(f"listed on website but CRM status is {s['status']}")
        s_sop = sop(s, BELLHAVEN_ID)
        if s_sop["changes_parent"]:
            reasons.append(f"wrong parent ({s['parent_name'] or 'none'} -> Bellhaven)")

        ev_base = {"web": _web_view(w), "match_basis": match_basis, "name_similarity": sim,
                   "crm_before": _view(s), "sop": s_sop,
                   "signals": {"street_equal_normalized": not street_diff, "zip_equal": not zip_diff,
                               "city_state_equal": True,
                               "administrator_is_crm_contact": _admin_hit(w, s, names),
                               "care_type_web_vs_crm": f"{care_type(w['care_offerings'])} vs {s['care_type']}"}}

        if s_sop["chow_required"]:
            new_fields = {"name": w["name"], "parent_id": BELLHAVEN_ID, "status": "Active",
                          "care_type": s["care_type"] or care_type(w["care_offerings"]),
                          "phone": w.get("phone") or s["phone"],
                          "billing_street": w["street"], "billing_city": w["city"],
                          "billing_state": w["state"], "billing_zip": w["zip"],
                          "note": f"CHOW successor of {s['account_id']} ({s['name']}). Created by ownership sync."}
            add("CHOW", s["account_id"], {"parent_id": BELLHAVEN_ID},
                f"CHOW: {s['name']} -> new account under Bellhaven",
                [{"op": "create", "fields": new_fields},
                 {"op": "update", "account_id": s["account_id"],
                  "fields": {"chow_current_account": "$created:0"},
                  "expect": {"parent_id": s["parent_id"], "status": s["status"]}}],
                {**ev_base, "reasons": reasons,
                 "why_it_matters": "Facility now belongs to Bellhaven, but old account has billing history and open AR - "
                 "it must stay untouched for billing; sales needs a correctly-parented live account."},
                "high")
        elif fields or s_sop["changes_parent"]:
            if s_sop["changes_parent"]:
                fields["parent_id"] = BELLHAVEN_ID
            tags = []
            if "parent_id" in fields: tags.append("re-parent")
            if "name" in fields: tags.append("align name (format only)" if cosmetic_name else "rename")
            if "billing_street" in fields or "billing_zip" in fields: tags.append("fix address")
            if "status" in fields: tags.append("reactivate")
            add("FIX", s["account_id"], fields,
                f"{' + '.join(tags).capitalize()}: {s['name']} ({s['billing_city']}, {s['billing_state']})",
                [{"op": "update", "account_id": s["account_id"], "fields": fields,
                  "expect": {"parent_id": s["parent_id"], "name": s["name"], "status": s["status"]}}],
                {**ev_base, "reasons": reasons,
                 "why_it_matters": "Wrong parent/name/address breaks the facility->corporate link, so reps may "
                 "route outreach or contract sign-off to the wrong owner." if "parent_id" in fields else
                 "Stale name/address makes the account hard to find and breaks matching on future runs."
                 if set(fields) != {"name"} or not cosmetic_name else
                 "Same facility, same brand; the CRM spelling differs from the operator's published name. "
                 "Aligning it is zero-risk and makes the account findable by the name reps will hear."},
                "high" if "parent_id" in fields else ("low" if set(fields) == {"name"} and cosmetic_name else "medium"))
        else:
            confident.append({"web": _web_view(w), "crm": _view(s), "name_similarity": sim,
                              "note": "Address, parent and name all agree (street abbreviations only)."})

        # ---- duplicates ------------------------------------------------------------
        for d in losers:
            nc = contacts.get(d["account_id"], 0)
            blocked = (d["lifetime_revenue"] or 0) > 0 and (d["outstanding_ar"] or 0) > 0
            d_sop = {"revenue": d["lifetime_revenue"] or 0, "outstanding_ar": d["outstanding_ar"] or 0,
                     "changes_parent": False, "chow_required": False,
                     "rule": ("Duplicate copy is retired (Inactive + duplicate_of_account), not re-parented. "
                              + ("It has revenue AND AR, so it is NOT auto-retired - routed to Needs Review."
                                 if blocked else "No billing history is lost (revenue or AR is zero)."))}
            ev = {"web": _web_view(w), "survivor": _view(s), "survivor_chosen_because": survivor_why,
                  "cluster_size": len(cluster), "crm_before": _view(d),
                  "name_similarity_to_survivor": name_sim(d["name"], s["name"]),
                  "signals": {"same_normalized_street": True, "same_city_state": True},
                  "sop": d_sop, "active_contacts_on_this_copy": nc,
                  "why_it_matters": "Duplicate accounts split activity and revenue history, and one of them often "
                  "carries the wrong parent. Reps may work (or double-count) the same facility twice."}
            if blocked:
                add("REVIEW", d["account_id"], {"status": "Needs Review", "dup_of": s["account_id"]},
                    f"Possible duplicate with billing history: {d['name']}",
                    [{"op": "update", "account_id": d["account_id"],
                      "fields": {"status": "Needs Review",
                                 "note": f"Possible duplicate of {s['name']} ({s['account_id']}); has revenue AND AR - needs billing review."},
                      "expect": {"status": d["status"]}}], ev, "high")
            else:
                note = f"Duplicate of {s['name']} ({s['account_id']}) - same address. Consolidated by ownership sync."
                if nc:
                    note += f" NOTE: {nc} active contact(s) still on this copy."
                add("DUPLICATE", d["account_id"], {"duplicate_of_account": s["account_id"]},
                    f"Duplicate: {d['name']} -> {s['name']} ({w['city']}, {w['state']})",
                    [{"op": "update", "account_id": d["account_id"],
                      "fields": {"duplicate_of_account": s["account_id"], "status": "Inactive", "note": note},
                      "expect": {"status": d["status"], "parent_id": d["parent_id"]}}], ev, "medium")

    # ---- under Bellhaven in CRM but not on the website --------------------------
    # Two very different situations:
    #  (a) another owner's account already sits at the same address -> the facility was SOLD (divestiture).
    #      Same billing SOP as any parent change: revenue AND AR -> keep the old account untouched and only
    #      link it (chow_current_account) to the successor that already exists. No new account needed.
    #  (b) nothing else at that address -> closure or sale unknown -> Needs Review, parent untouched.
    for a in sorted(crm, key=lambda x: x["name"]):
        if (is_parent_account(a) or a["parent_id"] != BELLHAVEN_ID or a["account_id"] in claimed
                or a.get("chow_current_account") or a.get("duplicate_of_account")):
            continue
        our_flag = a["status"] == "Needs Review" and a["note"].startswith(REMOVED_NOTE_PREFIX)
        if a["status"] != "Active" and not our_flag:
            continue
        sop_info = sop(a, a["parent_id"])
        billing = (a["lifetime_revenue"] or 0) > 0 or (a["outstanding_ar"] or 0) > 0
        successors = [c for c in pool if c["account_id"] != a["account_id"] and c["status"] == "Active"
                      and c["parent_id"] not in ("", BELLHAVEN_ID)
                      and c["billing_state"] == a["billing_state"]
                      and c["billing_city"].lower() == a["billing_city"].lower()
                      and norm_street(c["billing_street"]) == norm_street(a["billing_street"])]
        if successors:
            succ = successors[0]
            owner = succ["parent_name"].replace(" (Parent Account)", "")
            chow = (a["lifetime_revenue"] or 0) > 0 and (a["outstanding_ar"] or 0) > 0
            fields = {"chow_current_account": succ["account_id"]} if chow else {
                "duplicate_of_account": succ["account_id"], "status": "Inactive",
                "note": f"Facility moved to {owner}; successor account {succ['account_id']} already exists. "
                        "No billing history to preserve."}
            if chow and our_flag:   # undo our earlier Needs Review flag: SOP says the old account stays exactly as is
                fields.update({"status": "Active", "note": ""})
            add("DIVEST", a["account_id"], {"successor": succ["account_id"], "chow": chow},
                f"Sold to {owner}: {a['name']} ({a['billing_city']}, {a['billing_state']})",
                [{"op": "update", "account_id": a["account_id"], "fields": fields,
                  "expect": {"parent_id": a["parent_id"], "status": a["status"]}}],
                {"crm_before": _view(a), "successor": _view(succ), "web": None,
                 "sop": {**sop_info, "chow_required": chow, "changes_parent": True,
                         "rule": (f"Facility is no longer Bellhaven's and belongs under {owner}. Revenue ${sop_info['revenue']:,} > 0 "
                                  f"AND AR ${sop_info['outstanding_ar']:,} > 0 -> old account must be preserved exactly as is; "
                                  "only chow_current_account is set, pointing at the existing successor (no duplicate account created)."
                                  if chow else
                                  "No billing history to preserve -> old copy retired as duplicate of the existing successor.")},
                 "basis": f"Not on Bellhaven's website, and an active {owner} account already exists at the same street/city/state.",
                 "why_it_matters": "Without this link billing can't tie past revenue/AR to the current owner, and reps would keep "
                                   "selling to a facility under Bellhaven corporate that Bellhaven no longer owns."},
                "high")
        elif a["status"] == "Active":
            add("REMOVED", a["account_id"], {"status": "Needs Review"},
                f"Not on website: {a['name']} ({a['billing_city']}, {a['billing_state']})",
                [{"op": "update", "account_id": a["account_id"],
                  "fields": {"status": "Needs Review", "note": REMOVED_NOTE_PREFIX + " - verify closure/divestiture before outreach. "
                                                               "Parent left unchanged (new owner unknown)."},
                  "expect": {"status": a["status"], "parent_id": a["parent_id"]}}],
                {"crm_before": _view(a), "sop": sop_info, "web": None,
                 "basis": "No website location shares this street/city, no same-city website name is similar, and no other owner's account sits at this address.",
                 "why_it_matters": (
                     ("Has billing history" + (" and OPEN AR - parent left as is; flag for billing/CSM." if (a['outstanding_ar'] or 0) > 0 else "."))
                     if billing else "Reps may keep prospecting a facility Bellhaven no longer operates.")
                 + " Status->Needs Review (not Inactive / not re-parented) because we cannot tell closure from sale."},
                "high" if billing else "medium")

    summary = defaultdict(int)
    for p in proposals:
        summary[p["kind"]] += 1
    summary["CONFIDENT"] = len(confident)
    return {"proposals": proposals, "confident": confident, "summary": dict(summary)}
