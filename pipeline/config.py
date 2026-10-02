"""Settings. Token comes from env / .env — never hardcode it."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


def _load_dotenv():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

SITE_BASE = os.getenv("SITE_BASE", "https://analyst-assessment-production.up.railway.app")
CRM_BASE = os.getenv("CRM_BASE", SITE_BASE + "/api/v1")
CRM_TOKEN = os.getenv("CRM_TOKEN", "")
DB_PATH = Path(os.getenv("DB_PATH", DATA / "pipeline.db"))
