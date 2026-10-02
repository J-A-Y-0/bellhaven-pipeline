"""Thin client for the CRM sandbox API."""
import requests
from . import config


class CRM:
    def __init__(self, token=None, base=None):
        self.base = base or config.CRM_BASE
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token or config.CRM_TOKEN}"

    def _check(self, r):
        if not r.ok:
            raise RuntimeError(f"CRM {r.request.method} {r.url} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def all_accounts(self) -> list[dict]:
        out, page = [], 1
        while True:
            d = self._check(self.s.get(f"{self.base}/accounts",
                                       params={"page": page, "page_size": 100}, timeout=30))
            out += d["data"]
            if len(out) >= d["total"] or not d["data"]:
                return out
            page += 1

    def get(self, account_id):
        return self._check(self.s.get(f"{self.base}/accounts/{account_id}", timeout=30))

    def create(self, fields: dict):
        return self._check(self.s.post(f"{self.base}/accounts", json=fields, timeout=30))

    def update(self, account_id, fields: dict):
        return self._check(self.s.patch(f"{self.base}/accounts/{account_id}", json=fields, timeout=30))

    def contacts(self) -> tuple[dict, dict]:
        """Active contacts -> (count per account, names per account)."""
        counts, names, page = {}, {}, 1
        while True:
            d = self._check(self.s.get(f"{self.base}/contacts", params={"page": page, "page_size": 100}, timeout=30))
            for c in d["data"]:
                if c.get("is_active", True):
                    counts[c["account_id"]] = counts.get(c["account_id"], 0) + 1
                    names.setdefault(c["account_id"], []).append(c["name"])
            if page * 100 >= d["total"] or not d["data"]:
                return counts, names
            page += 1
