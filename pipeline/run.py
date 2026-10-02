"""Daily job: scrape -> snapshot CRM -> propose. Never writes to the CRM."""
from . import scraper, store
from .crm import CRM
from .matcher import propose


def main():
    crm = CRM()
    web, accts = scraper.scrape(), crm.all_accounts()
    contacts, names = crm.contacts()
    if len(web) < 20:  # a broken scrape must not make every CRM account look 'removed'
        raise SystemExit(f"Only {len(web)} locations scraped - refusing to propose. Check the site/scraper.")
    db = store.connect()
    last = db.execute("SELECT web_count FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if last and len(web) < 0.8 * last["web_count"]:  # site outage / layout change
        raise SystemExit(f"Website location count fell from {last['web_count']} to {len(web)} - refusing to propose.")
    out = propose(web, accts, contacts, names)
    run_id = store.record_run(db, len(web), len(accts), out["summary"])
    res = store.upsert_proposals(db, run_id, out["proposals"])
    print(f"run {run_id}: website={len(web)} crm={len(accts)} | {out['summary']} | {res}")
    return res


if __name__ == "__main__":
    main()
