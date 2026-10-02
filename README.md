# Bellhaven ownership sync

Keeps CRM facility -> parent links in line with Bellhaven's website: scrape, match, human review, write back.
Nothing is written to the CRM without an approval in the review app.

**End state:** all 35 Bellhaven locations map to exactly one active CRM account under the Bellhaven parent; the billing
SOP held for every change of ownership; re-runs propose nothing. `python verify_end_state.py` proves it against the live CRM.

## Run
Python 3.10+.
```bash
pip install -r requirements.txt
cp .env.example .env                     # add CRM_TOKEN
python -m pipeline.run                   # scrape + read CRM -> pending proposals (read-only)
uvicorn review_app.app:app --port 8000   # review at http://localhost:8000; Approve writes to the CRM
python -m pytest -q tests                # 34 tests
python verify_end_state.py               # independent audit; exits 1 on any violation
```
Daily schedule: `schedule/crontab`, on the host that runs the review app. Both use the same `data/pipeline.db`, which holds
every decision; sharing it is what stops approved or rejected items from being re-proposed. The daily run only proposes.

## Layout
| | |
|---|---|
| `pipeline/scraper.py` | paginated directory + homepage links (Findlay is only announced on the homepage) |
| `pipeline/matcher.py` | matching, classification, billing SOP; pure functions, every proposal carries evidence |
| `pipeline/store.py` | SQLite proposals + decisions; fingerprints make re-runs idempotent |
| `pipeline/apply.py` | write-back: re-reads the live account, refuses stale proposals, re-checks the SOP |
| `pipeline/crm.py`, `run.py`, `config.py` | API client, daily entry point, settings |
| `review_app/` | FastAPI + one page: diff, evidence, SOP check, approve / reject |
| `schedule/crontab` | daily run |
| `verify_end_state.py`, `fixtures/` | end-state audit with its own address logic; CRM snapshot from before any write |

## Proposal types
| Type | When | Write |
|---|---|---|
| `FIX` | matched, but parent / name / address / ZIP differs | update in place (parent only if the SOP allows) |
| `CHOW` | parent change on an account with revenue **and** AR | create account under Bellhaven; old account gets only `chow_current_account` |
| `CREATE` | on the website, no CRM account | create under Bellhaven |
| `DUPLICATE` | extra account at the same address | `duplicate_of_account` + Inactive |
| `DIVEST` | under Bellhaven, off the website, another owner's account at the address | revenue **and** AR: link via `chow_current_account`; otherwise retire as a duplicate of the successor |
| `REMOVED` | under Bellhaven, off the website, no successor | Needs Review + note, parent untouched |
| `REVIEW` | duplicate that has revenue **and** AR | Needs Review, never auto-retired |

## Judgment calls
- **Sandusky** was sold: Millstone's account already sits at its address. The SOP asks for a new account under the correct parent; one already existed, so the old account (revenue $130k, AR $5.2k) is untouched except `chow_current_account` pointing at it.
- **Union Square** is a different facility from Juniper's `Union Square Senior Living`: different street, name and owner. Bellhaven only announced acquisitions from Harborview and Cedar Trail, and every rebrand in the data kept its address. The website's administrator isn't a contact anywhere in the CRM. A wrong create is one fixable duplicate; a wrong merge would move another owner's facility.
- **Alliance, Coldwater**: off the website with no successor anywhere in the CRM, so closure can't be told from sale -> Needs Review, not Inactive.
- **Duplicate survivors** rank billing history > website administrator on record > already under Bellhaven > contacts. The administrator confirms the pick in Owosso, Erie and Port Clinton. Kettering (no signal) keeps the Harborview copy: Harborview joined whole, Cedar Trail only partly.
- **Names follow the website**, including four format-only variants (zero risk). Street abbreviations are left alone: same address, and billing data shouldn't churn for formatting.
- **Ashtabula**'s `PO Box` billing street became the street address; no revenue, so no invoices at risk.
- **Not synced:** phones (differ everywhere, no way to tell which is right). Contacts on retired duplicates are not moved.

## Safety
- The SOP is checked when proposing and again at write time against the live account.
- Before the first write, the whole proposal is re-validated against the live CRM: stale proposals are refused, and a
  create is refused if an account has appeared at that address. A CHOW retry never creates a second account.
- A known fingerprint is never re-proposed, whatever its decision; pending items the data outgrew become obsolete.
- Bulk approval covers only low/medium-risk proposals (name/address fixes, duplicates, flags on accounts without billing
  history). Re-parents, CHOWs, divestitures, new accounts and anything with billing history are approved one by one.
- The run refuses to propose on a suspicious scrape (< 20 locations, a > 20% drop, or a page that parses incompletely).
