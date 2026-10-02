"""Scrape every Bellhaven community from the public website."""
import re
import time
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup

from . import config

HEADERS = {"User-Agent": "bellhaven-ownership-sync/1.0 (analyst assessment)"}


def _get(path: str) -> str:
    for attempt in range(4):
        try:
            r = requests.get(config.SITE_BASE + path, headers=HEADERS, timeout=45)
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def list_slugs() -> list[str]:
    """Walk the paginated directory ('Page n / N') and collect detail-page slugs."""
    slugs, page, last = [], 1, 1
    while page <= last:
        soup = BeautifulSoup(_get(f"/communities?page={page}"), "html.parser")
        m = re.search(r"Page\s+(\d+)\s*/\s*(\d+)", soup.get_text(" "))
        if m:
            last = int(m.group(2))
        for a in soup.select(".card h3 a"):
            s = a["href"].rsplit("/", 1)[-1]
            if s not in slugs:
                slugs.append(s)
        page += 1
    return slugs


def parse_detail(slug: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    name = soup.find("h1").get_text(strip=True)
    fields = {}
    for dt in soup.select("dl.detail dt"):
        dd = dt.find_next_sibling("dd")
        fields[dt.get_text(strip=True).lower()] = dd
    addr_dd = fields.get("address")
    street = city = state = zip_ = ""
    if addr_dd:
        lines = [t.strip() for t in addr_dd.get_text("\n").split("\n") if t.strip()]
        street = lines[0] if lines else ""
        if len(lines) > 1:
            m = re.match(r"(.+?),\s*([A-Z]{2})\s+(\d{5})", lines[1])
            if m:
                city, state, zip_ = m.groups()
    care = []
    cd = fields.get("care offerings")
    if cd:
        care = [b.get_text(strip=True) for b in cd.select(".badge")] or \
               [t.strip() for t in cd.get_text("\n").split("\n") if t.strip()]
    phone = fields["phone"].get_text(strip=True) if fields.get("phone") else ""
    administrator = fields["administrator"].get_text(strip=True) if fields.get("administrator") else ""
    missing = [k for k, v in {"name": name, "street": street, "city": city, "state": state, "zip": zip_}.items() if not v]
    if missing:   # fail loudly: a half-parsed page must never look like 'location changed / removed'
        raise ValueError(f"{slug}: could not parse {missing}")
    return {"slug": slug, "name": name, "street": street, "city": city, "state": state,
            "zip": zip_, "care_offerings": care, "phone": phone, "administrator": administrator,
            "url": f"{config.SITE_BASE}/communities/{slug}"}


def home_slugs() -> list[str]:
    """Pages linked from the homepage. Catches communities that are announced there
    (e.g. 'New this year') but have not been added to the paginated directory yet."""
    soup = BeautifulSoup(_get("/"), "html.parser")
    return [a["href"].rsplit("/", 1)[-1] for a in soup.select('a[href^="/communities/"]')]


def scrape() -> list[dict]:
    directory = list_slugs()
    extra = [s for s in dict.fromkeys(home_slugs()) if s not in directory]  # de-duplicated, order kept
    def one(s):
        rec = parse_detail(s, _get(f"/communities/{s}"))
        rec["source"] = "directory" if s in directory else "homepage_only"
        return rec
    with ThreadPoolExecutor(max_workers=6) as ex:   # site is slow (~3s/page); stay polite but parallel
        return list(ex.map(one, directory + extra))


if __name__ == "__main__":
    import json
    rows = scrape()
    print(json.dumps(rows[:3], indent=1))
    print(len(rows), "locations")
