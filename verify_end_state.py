"""Independent audit of the CRM end state against the website.

Deliberately does NOT reuse the matcher: it keys addresses differently (house number + first street word,
ignoring city and directionals - broad on purpose: it can raise a false alarm, never hide a twin) and checks
invariants the corrected CRM must satisfy. Exit code 1 on any issue.
Read-only. Usage:  python verify_end_state.py
"""
import json
import re
import sys

from pipeline import config, scraper
from pipeline.crm import CRM
from pipeline.matcher import BELLHAVEN_ID as BH

DIRECTIONALS = {"n", "s", "e", "w", "north", "south", "east", "west", "nw", "ne", "sw", "se", "northwest",
                "northeast", "southwest", "southeast"}


def addr_key(street: str, state: str):
    toks = re.sub(r"[^a-z0-9 ]", " ", street.lower()).split()
    if not toks or not toks[0].isdigit():
        return None
    rest = [t for t in toks[1:] if t not in DIRECTIONALS]
    return toks[0], (rest[0] if rest else ""), state


def is_effective(a):
    return a["status"] == "Active" and not a["duplicate_of_account"] and not a["chow_current_account"]


def main():
    web, accts = scraper.scrape(), CRM().all_accounts()
    by_id = {a["account_id"]: a for a in accts}
    # CRM as it was before this sync touched it (committed, so the audit is reproducible from a clean clone)
    snap = config.ROOT / "fixtures" / "crm_before_sync.json"
    initial = {a["account_id"]: a for a in json.load(open(snap))}
    issues = []

    # 1. every website location: exactly one effective account, under Bellhaven, same city/ZIP, website name
    web_keys = set()
    for w in web:
        k = addr_key(w["street"], w["state"])
        web_keys.add(k)
        here = [a for a in accts if addr_key(a["billing_street"], a["billing_state"]) == k]
        eff = [a for a in here if is_effective(a)]
        if len(eff) != 1:
            issues.append(f"{w['name']}: {len(eff)} effective accounts")
            continue
        e = eff[0]
        if e["parent_id"] != BH: issues.append(f"{w['name']}: parent is {e['parent_name'] or 'none'}")
        if e["billing_city"].lower() != w["city"].lower() or e["billing_zip"] != w["zip"]:
            issues.append(f"{w['name']}: city/ZIP {e['billing_city']} {e['billing_zip']} != {w['city']} {w['zip']}")
        if e["name"] != w["name"]: issues.append(f"{w['name']}: CRM name is '{e['name']}'")
        # 2. every other account at that address is explained
        for a in here:
            if a is e:
                continue
            if a["duplicate_of_account"]:
                if a["duplicate_of_account"] != e["account_id"] or a["status"] != "Inactive":
                    issues.append(f"duplicate {a['name']} ({a['account_id']}) not Inactive/pointing at survivor")
            elif not a["chow_current_account"]:
                issues.append(f"unexplained extra account at {w['name']}: {a['name']} ({a['status']})")

    # 3. CHOW: old account had revenue AND AR, is untouched except the link, target is live at same address
    for a in accts:
        if not a["chow_current_account"]:
            continue
        t = by_id.get(a["chow_current_account"])
        i = initial.get(a["account_id"])
        if not t or not is_effective(t) or addr_key(t["billing_street"], t["billing_state"]) != addr_key(a["billing_street"], a["billing_state"]):
            issues.append(f"CHOW {a['name']}: target missing, inactive or at another address")
        if i:
            if not (i["lifetime_revenue"] > 0 and i["outstanding_ar"] > 0):
                issues.append(f"CHOW {a['name']}: used without revenue AND AR")
            changed = {k for k in a if k not in ("updated_at", "chow_current_account") and a[k] != i[k]}
            if changed: issues.append(f"CHOW old account {a['name']} modified: {sorted(changed)}")
        print(f"CHOW  {a['name']:30} [{(a['parent_name'] or '-')[:22]}, rev ${a['lifetime_revenue']:,}, AR ${a['outstanding_ar']:,}] "
              f"-> {t['account_id'] if t else '?'} [{(t or {}).get('parent_name', '')[:22]}]")

    # 4. Bellhaven-parented accounts that aren't on the website: CHOW-linked to the new owner or flagged
    for a in accts:
        if a["parent_id"] == BH and addr_key(a["billing_street"], a["billing_state"]) not in web_keys:
            if a["chow_current_account"]:
                continue  # sold; SOP keeps the old account as is
            if a["status"] == "Active":
                issues.append(f"{a['name']} is Active under Bellhaven but not on the website")
            print(f"OFF-SITE  {a['name']:30} status={a['status']}")

    # 5. billing SOP across the whole CRM + no out-of-scope edits
    for a in accts:
        i = initial.get(a["account_id"])
        if not i:
            continue
        if i["parent_id"] != a["parent_id"] and i["lifetime_revenue"] > 0 and i["outstanding_ar"] > 0:
            issues.append(f"SOP violated: {a['name']} re-parented with revenue AND AR")
        if a["duplicate_of_account"] and i["lifetime_revenue"] > 0 and i["outstanding_ar"] > 0:
            issues.append(f"{a['name']} retired as duplicate despite revenue AND AR (billing needs it)")
        changed = {k for k in a if k != "updated_at" and a[k] != i[k]}
        if changed and i["parent_id"] != BH and addr_key(a["billing_street"], a["billing_state"]) not in web_keys:
            issues.append(f"out-of-scope edit: {a['name']} {sorted(changed)}")

    # 6. every account this sync created has no unexplained twin anywhere in the CRM
    for a in accts:
        if a["account_id"] not in initial:
            twins = [b for b in accts if b is not a and addr_key(b["billing_street"], b["billing_state"]) == addr_key(a["billing_street"], a["billing_state"])
                     and b["chow_current_account"] != a["account_id"]]
            if twins: issues.append(f"created {a['name']} has twin(s): {[b['name'] for b in twins]}")

    created = len([a for a in accts if a["account_id"] not in initial])
    changed = len([a for a in accts if a["account_id"] in initial
                   and any(a[k] != initial[a["account_id"]][k] for k in a if k != "updated_at")])
    print(f"\nwebsite {len(web)} | CRM {len(accts)} | created {created} | modified {changed}")
    print("ISSUES:", "\n  - ".join([""] + issues) if issues else "none - all invariants hold")
    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
