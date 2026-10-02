import json
import pytest
from pipeline import matcher, store
from pipeline.apply import _guard, apply_proposal, SOPViolation, StaleProposal
from pipeline.matcher import BELLHAVEN_ID, norm_street, norm_name, sop, propose

BH = BELLHAVEN_ID


def acct(id, name, street, city, st, zip_, parent=BH, rev=0, ar=0, status="Active", **kw):
    return {"account_id": id, "name": name, "parent_id": parent, "parent_name": "P" if parent else "",
            "billing_street": street, "billing_city": city, "billing_state": st, "billing_zip": zip_,
            "care_type": "Skilled Nursing", "status": status, "phone": "", "lifetime_revenue": rev,
            "outstanding_ar": ar, "chow_current_account": "", "duplicate_of_account": "", "note": "", **kw}


def web(name, street, city, st, zip_, slug="x"):
    return {"slug": slug, "name": name, "street": street, "city": city, "state": st, "zip": zip_,
            "care_offerings": ["Assisted Living"], "phone": "", "url": "u", "source": "directory"}


PARENT = acct("PARENT", "Bellhaven Senior Living (Parent Account)", "", "", "", "", parent="")
OTHER = "OTHERPARENT"


def kinds(out):
    return sorted(p["kind"] for p in out["proposals"])


@pytest.mark.parametrize("site, crm", [   # real website vs CRM pairs from this dataset
    ("1250 NW Franklin Street", "1250 Northwest Franklin St"),
    ("199 Barks Rd W", "199 Barks Road West"),
    ("4850 NW Sylvania Ave", "4850 Northwest Sylvania Avenue"),
    ("3313 Wilmington Pike", "3313 Wilmington Pk"),
    ("1120 W Main St", "1120 West Main Street"),
])
def test_street_normalization(site, crm):
    assert norm_street(site) == norm_street(crm)


def test_street_normalization_keeps_different_streets_apart():
    assert norm_street("118 Union Square Dr") != norm_street("240 Market St")
    assert norm_street("2715 Columbus Ave") != norm_street("2715 Columbus Rd")


def test_name_variants_are_recognised_as_format_only():
    assert matcher.name_sim("Bellhaven at Sycamore Ridge", "Bellhaven of Sycamore Ridge") >= matcher.COSMETIC_SIM
    assert matcher.name_sim("Bellhaven Rehabilitation & Nursing of Grove City", "Bellhaven Rehab and Nursing of Grove City") >= matcher.COSMETIC_SIM
    assert matcher.name_sim("Bellhaven of Chagrin Falls", "Riverbend Manor Care Center") < matcher.COSMETIC_SIM


# ---- SOP ---------------------------------------------------------------------
def test_sop_requires_chow_only_with_revenue_and_ar():
    assert sop(acct("a", "n", "s", "c", "OH", "1", parent=OTHER, rev=100, ar=1), BH)["chow_required"]
    assert not sop(acct("a", "n", "s", "c", "OH", "1", parent=OTHER, rev=100, ar=0), BH)["chow_required"]
    assert not sop(acct("a", "n", "s", "c", "OH", "1", parent=OTHER, rev=0, ar=50), BH)["chow_required"]
    assert not sop(acct("a", "n", "s", "c", "OH", "1", parent=BH, rev=100, ar=50), BH)["chow_required"]  # no move


def test_chow_proposal_leaves_old_account_untouched():
    crm = [PARENT, acct("OLD", "Bellhaven of X", "1 Main St", "X", "OH", "11111", parent=OTHER, rev=500, ar=40)]
    out = propose([web("Bellhaven of X", "1 Main Street", "X", "OH", "11111")], crm)
    assert kinds(out) == ["CHOW"]
    ops = out["proposals"][0]["actions"]
    assert ops[0]["op"] == "create" and ops[0]["fields"]["parent_id"] == BH
    assert ops[1]["account_id"] == "OLD" and list(ops[1]["fields"]) == ["chow_current_account"]


def test_no_revenue_or_no_ar_is_direct_reparent():
    for rev, ar in [(0, 0), (500, 0), (0, 40)]:
        crm = [PARENT, acct("A", "Bellhaven of X", "1 Main St", "X", "OH", "11111", parent=OTHER, rev=rev, ar=ar)]
        out = propose([web("Bellhaven of X", "1 Main St", "X", "OH", "11111")], crm)
        assert kinds(out) == ["FIX"] and out["proposals"][0]["actions"][0]["fields"]["parent_id"] == BH


def test_guard_blocks_reparent_even_if_proposal_is_wrong():
    live = acct("A", "n", "s", "c", "OH", "1", parent=OTHER, rev=10, ar=10)
    with pytest.raises(SOPViolation):
        _guard(live, {"parent_id": BH}, {})
    with pytest.raises(StaleProposal):
        _guard(live, {"name": "z"}, {"parent_id": "SOMETHING_ELSE"})


# ---- matching ------------------------------------------------------------------
def test_name_alone_never_matches_across_cities():
    crm = [PARENT, acct("A", "Bellhaven of New Carlisle", "875 Elm St", "New Carlisle", "OH", "45344")]
    out = propose([web("Bellhaven of Carlisle", "640 Walnut Bottom Rd", "Carlisle", "PA", "17015")], crm)
    assert "CREATE" in kinds(out)
    assert any(p["kind"] == "REMOVED" for p in out["proposals"])  # New Carlisle isn't on this (toy) site


def test_same_name_other_city_is_not_a_match():
    crm = [PARENT, acct("A", "Amberly Manor", "918 S Nevada Ave", "Colorado Springs", "CO", "80903", parent=OTHER)]
    out = propose([web("Amberly Manor", "4390 Darrow Rd", "Hudson", "OH", "44236")], crm)
    assert kinds(out) == ["CREATE"]
    assert out["proposals"][0]["evidence"]["same_name_in_other_cities_ignored"]


def test_zip_typo_and_po_box_are_fixes_not_new_accounts():
    crm = [PARENT, acct("A", "Bellhaven of P", "2 Gallia St", "P", "OH", "45626"),
           acct("B", "Bellhaven of Q", "PO Box 5", "Q", "OH", "44004")]
    out = propose([web("Bellhaven of P", "2 Gallia St", "P", "OH", "45662", "p"),
                   web("Bellhaven of Q", "3156 W Prospect Rd", "Q", "OH", "44004", "q")], crm)
    assert kinds(out) == ["FIX", "FIX"]
    f = {p["actions"][0]["account_id"]: p["actions"][0]["fields"] for p in out["proposals"]}
    assert f["A"] == {"billing_zip": "45662"} and f["B"] == {"billing_street": "3156 W Prospect Rd"}


def test_duplicate_cluster_keeps_one_and_retires_others():
    crm = [PARENT, acct("KEEP", "Bellhaven of D", "9 Elm St", "D", "OH", "1"),
           acct("DUP", "Harborview D", "9 Elm Street", "D", "OH", "1", parent=OTHER)]
    out = propose([web("Bellhaven of D", "9 Elm St", "D", "OH", "1")], crm)
    assert kinds(out) == ["DUPLICATE"]
    f = out["proposals"][0]["actions"][0]["fields"]
    assert f["duplicate_of_account"] == "KEEP" and f["status"] == "Inactive"


def test_duplicate_with_revenue_and_ar_is_not_auto_retired():
    crm = [PARENT, acct("KEEP", "Bellhaven of D", "9 Elm St", "D", "OH", "1", rev=5),
           acct("DUP", "Harborview D", "9 Elm St", "D", "OH", "1", parent=OTHER, rev=9, ar=9)]
    out = propose([web("Bellhaven of D", "9 Elm St", "D", "OH", "1")], crm)
    assert kinds(out) == ["REVIEW"]


def test_removed_account_is_flagged_not_reparented():
    crm = [PARENT, acct("GONE", "Bellhaven of Gone", "1 A St", "Gone", "OH", "9", rev=100, ar=10),
           acct("HERE", "Bellhaven of Here", "2 B St", "Here", "OH", "8")]
    out = propose([web("Bellhaven of Here", "2 B St", "Here", "OH", "8")], crm)
    assert kinds(out) == ["REMOVED"]
    assert out["proposals"][0]["actions"][0]["fields"]["status"] == "Needs Review"
    assert "parent_id" not in out["proposals"][0]["actions"][0]["fields"]


def test_superseded_accounts_are_ignored_after_chow_and_dup():
    crm = [PARENT,
           acct("OLD", "Bellhaven of X", "1 Main St", "X", "OH", "1", parent=OTHER, rev=5, ar=5, chow_current_account="NEW"),
           acct("NEW", "Bellhaven of X", "1 Main St", "X", "OH", "1"),
           acct("D", "Dup", "1 Main St", "X", "OH", "1", status="Inactive", duplicate_of_account="NEW")]
    assert propose([web("Bellhaven of X", "1 Main St", "X", "OH", "1")], crm)["proposals"] == []


# ---- idempotency ---------------------------------------------------------------
def test_rerun_does_not_repropose_decided_items(tmp_path):
    db = store.connect(tmp_path / "t.db")
    crm = [PARENT, acct("A", "Bellhaven of P", "2 Gallia St", "P", "OH", "45626")]
    w = [web("Bellhaven of P", "2 Gallia St", "P", "OH", "45662")]
    props = propose(w, crm)["proposals"]
    r1 = store.upsert_proposals(db, store.record_run(db, 1, 2, {}), props)
    assert r1["new"] == 1
    fp = props[0]["fingerprint"]
    db.execute("UPDATE proposals SET status='rejected' WHERE fingerprint=?", (fp,)); db.commit()
    r2 = store.upsert_proposals(db, store.record_run(db, 1, 2, {}), propose(w, crm)["proposals"])
    assert r2["new"] == 0 and r2["already_known"] == 1
    assert db.execute("SELECT status FROM proposals").fetchone()[0] == "rejected"   # decision survives


def test_pending_becomes_obsolete_when_data_converges(tmp_path):
    db = store.connect(tmp_path / "t.db")
    crm = [PARENT, acct("A", "Bellhaven of P", "2 Gallia St", "P", "OH", "45626")]
    w = [web("Bellhaven of P", "2 Gallia St", "P", "OH", "45662")]
    store.upsert_proposals(db, 1, propose(w, crm)["proposals"])
    crm[1]["billing_zip"] = "45662"   # someone fixed it manually
    res = store.upsert_proposals(db, 2, propose(w, crm)["proposals"])
    assert res["obsolete"] == 1


class FakeCRM:
    def __init__(self, accts): self.a = {x["account_id"]: dict(x) for x in accts}; self.creates = 0
    def get(self, i): return dict(self.a[i])
    def update(self, i, f): self.a[i].update(f)
    def create(self, f):
        self.creates += 1; i = f"NEW{self.creates}"
        self.a[i] = {"account_id": i, "duplicate_of_account": "", "chow_current_account": "", **f}
        return {"account_id": i}
    def all_accounts(self): return [dict(x) for x in self.a.values()]


def test_chow_apply_is_retry_safe():
    old = acct("OLD", "Bellhaven of X", "1 Main St", "X", "OH", "1", parent=OTHER, rev=5, ar=5)
    p = propose([web("Bellhaven of X", "1 Main St", "X", "OH", "1")], [PARENT, old])["proposals"][0]
    crm = FakeCRM([PARENT, old])
    res = apply_proposal(crm, {**p, "result": None})
    assert crm.creates == 1 and crm.a["OLD"]["chow_current_account"] == "NEW1" and crm.a["OLD"]["parent_id"] == OTHER
    apply_proposal(crm, {**p, "result": res})          # retry -> no second account
    assert crm.creates == 1


# ---- divestiture (facility left Bellhaven; another owner's account already at the address) ----
def test_divest_with_billing_links_existing_successor_and_creates_nothing():
    crm = [PARENT, acct("OLD", "Bellhaven of S", "2715 Columbus Ave", "S", "OH", "1", rev=130000, ar=5200),
           acct("NEWOWNER", "Millstone Care of S", "2715 Columbus Ave", "S", "OH", "1", parent="MILL")]
    out = propose([], crm)   # website no longer lists it
    assert kinds(out) == ["DIVEST"]
    acts = out["proposals"][0]["actions"]
    assert len(acts) == 1 and acts[0]["op"] == "update" and acts[0]["account_id"] == "OLD"
    assert acts[0]["fields"] == {"chow_current_account": "NEWOWNER"}   # old account otherwise untouched


def test_divest_undoes_our_earlier_needs_review_flag():
    crm = [PARENT, acct("OLD", "Bellhaven of S", "2715 Columbus Ave", "S", "OH", "1", rev=9, ar=9,
                        status="Needs Review", note=matcher.REMOVED_NOTE_PREFIX + " - verify"),
           acct("NEWOWNER", "Millstone Care of S", "2715 Columbus Ave", "S", "OH", "1", parent="MILL")]
    f = propose([], crm)["proposals"][0]["actions"][0]["fields"]
    assert f == {"chow_current_account": "NEWOWNER", "status": "Active", "note": ""}


def test_divest_without_billing_history_retires_old_copy():
    crm = [PARENT, acct("OLD", "Bellhaven of S", "2715 Columbus Ave", "S", "OH", "1", rev=0, ar=0),
           acct("NEWOWNER", "Millstone Care of S", "2715 Columbus Ave", "S", "OH", "1", parent="MILL")]
    f = propose([], crm)["proposals"][0]["actions"][0]["fields"]
    assert f["duplicate_of_account"] == "NEWOWNER" and f["status"] == "Inactive"


def test_removed_without_successor_stays_needs_review():
    crm = [PARENT, acct("OLD", "Bellhaven of S", "2715 Columbus Ave", "S", "OH", "1", rev=9, ar=9)]
    assert kinds(propose([], crm)) == ["REMOVED"]


def test_names_align_to_website_and_label_format_only_vs_rebrand():
    crm = [PARENT, acct("A", "Bellhaven of X", "1 Main St", "X", "OH", "1")]
    assert propose([web("Bellhaven of X", "1 Main St", "X", "OH", "1")], crm)["proposals"] == []   # identical -> nothing
    crm[1]["name"] = "Bellhaven at X"                                  # format-only difference
    p = propose([web("Bellhaven of X", "1 Main St", "X", "OH", "1")], crm)["proposals"][0]
    assert p["actions"][0]["fields"] == {"name": "Bellhaven of X"} and p["severity"] == "low" and "format only" in p["title"]
    crm[1]["name"] = "Riverbend Manor Care Center"                     # real rebrand
    p = propose([web("Bellhaven of X", "1 Main St", "X", "OH", "1")], crm)["proposals"][0]
    assert p["severity"] == "medium" and p["title"].startswith("Rename")


def test_stale_proposal_is_refused_not_written(tmp_path):
    from pipeline.apply import decide
    db = store.connect(tmp_path / "t.db")
    crm_rows = [PARENT, acct("A", "Bellhaven of P", "2 Gallia St", "P", "OH", "45626", parent=OTHER)]
    p = propose([web("Bellhaven of P", "2 Gallia St", "P", "OH", "45662")], crm_rows)["proposals"][0]
    store.upsert_proposals(db, 1, [p])
    fake = FakeCRM(crm_rows)
    fake.a["A"]["parent_id"] = "SOMEONE_ELSE"          # CRM changed after the proposal was made
    out = decide(db, fake, p["fingerprint"], "approve")
    assert out["status"] == "failed" and "Re-run" in out["error"]
    assert fake.a["A"]["billing_zip"] == "45626"        # nothing was written


# ---- administrator evidence -------------------------------------------------
def webadm(name, street, city, st, zip_, adm, slug="x"):
    return {**web(name, street, city, st, zip_, slug), "administrator": adm}


def test_same_city_account_without_admin_evidence_is_not_matched():      # the Union Square case
    crm = [PARENT, acct("J", "Union Square Senior Living", "240 Market St", "New Albany", "OH", "43054", parent=OTHER)]
    out = propose([webadm("Bellhaven at Union Square", "118 Union Square Dr", "New Albany", "OH", "43054", "Phil Holloway")],
                  crm, {}, {"J": ["Dale Croft"]})
    assert kinds(out) == ["CREATE"]
    near = out["proposals"][0]["evidence"]["near_misses_rejected"][0]
    assert near["administrator_is_crm_contact"] is False and near["contacts_on_account"] == ["Dale Croft"]


def test_administrator_contact_proves_identity_even_if_street_and_name_differ():
    crm = [PARENT, acct("J", "Union Square Senior Living", "240 Market St", "New Albany", "OH", "43054", parent=OTHER)]
    out = propose([webadm("Bellhaven at Union Square", "118 Union Square Dr", "New Albany", "OH", "43054", "Phil Holloway")],
                  crm, {}, {"J": ["Phil Holloway"]})
    assert kinds(out) == ["FIX"]
    f = out["proposals"][0]["actions"][0]["fields"]
    assert f["parent_id"] == BH and f["name"] == "Bellhaven at Union Square" and f["billing_street"] == "118 Union Square Dr"


def test_survivor_is_the_copy_holding_the_administrator_contact():
    crm = [PARENT, acct("A", "Bellhaven of D", "9 Elm St", "D", "OH", "1"),
           acct("B", "Bellhaven of D", "9 Elm Street", "D", "OH", "1")]
    out = propose([webadm("Bellhaven of D", "9 Elm St", "D", "OH", "1", "Gloria L")], crm, {}, {"B": ["Gloria L"]})
    assert out["proposals"][0]["actions"][0]["account_id"] == "A"          # A is the loser
    assert out["proposals"][0]["evidence"]["survivor"]["account_id"] == "B"



def test_failed_chow_is_retried_through_decide_without_a_second_account(tmp_path):
    """Create succeeds, link update fails -> retry via the approval path must reuse the created account."""
    from pipeline.apply import decide
    db = store.connect(tmp_path / "t.db")
    old = acct("OLD", "Bellhaven of X", "1 Main St", "X", "OH", "1", parent=OTHER, rev=5, ar=5)
    p = propose([web("Bellhaven of X", "1 Main St", "X", "OH", "1")], [PARENT, old])["proposals"][0]
    store.upsert_proposals(db, 1, [p])

    class Flaky(FakeCRM):
        fail = True
        def update(self, i, f):
            if Flaky.fail:
                Flaky.fail = False
                raise RuntimeError("network blip")
            super().update(i, f)

    crm = Flaky([PARENT, old])
    assert decide(db, crm, p["fingerprint"], "approve")["status"] == "failed"
    assert decide(db, crm, p["fingerprint"], "approve")["status"] == "applied"
    assert crm.creates == 1 and crm.a["OLD"]["chow_current_account"] == "NEW1"


def test_name_fallback_cannot_steal_another_locations_address_match():
    # X sits at web2's address but carries web1's name. web1 sorts first; address matches must win.
    crm = [PARENT, acct("X", "Bellhaven of Q", "9 Elm St", "Q", "OH", "1")]
    web1 = web("Bellhaven of Q", "1 New Rd", "Q", "OH", "1", "w1")
    web2 = web("Zed House of Q", "9 Elm St", "Q", "OH", "1", "w2")
    out = propose([web1, web2], crm)
    by_kind = {p["kind"]: p for p in out["proposals"]}
    assert sorted(by_kind) == ["CREATE", "FIX"]
    assert by_kind["FIX"]["actions"][0]["account_id"] == "X" and by_kind["FIX"]["actions"][0]["fields"] == {"name": "Zed House of Q"}
    assert by_kind["CREATE"]["actions"][0]["fields"]["name"] == "Bellhaven of Q"


def test_stale_chow_creates_nothing():
    """Old account changed after the proposal was made -> refused BEFORE the new account is created."""
    old = acct("OLD", "Bellhaven of X", "1 Main St", "X", "OH", "1", parent=OTHER, rev=5, ar=5)
    p = propose([web("Bellhaven of X", "1 Main St", "X", "OH", "1")], [PARENT, old])["proposals"][0]
    crm = FakeCRM([PARENT, old])
    crm.a["OLD"]["parent_id"] = "SOMEONE_ELSE"
    with pytest.raises(StaleProposal):
        apply_proposal(crm, p)
    assert crm.creates == 0


def test_create_refused_if_account_appeared_at_the_address():
    p = propose([web("Bellhaven of B", "2000 Hospital Dr", "B", "OH", "1")], [PARENT])["proposals"][0]
    crm = FakeCRM([PARENT, acct("MANUAL", "Bellhaven of Batavia", "2000 Hospital Drive", "B", "OH", "1")])
    with pytest.raises(StaleProposal):
        apply_proposal(crm, p)
    assert crm.creates == 0
