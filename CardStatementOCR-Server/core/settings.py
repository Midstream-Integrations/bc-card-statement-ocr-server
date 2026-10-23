"""
Settings for every module. Values come from environment variables; config/ocr.env is loaded
automatically (without overriding anything already set), so scripts and the services read the
same file.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # /opt/ocr
CONFIG_DIR = ROOT / "config"


def _load_env_file(path: Path):
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"").strip("\x27"))


_load_env_file(CONFIG_DIR / "ocr.env")

# ---- shared
DATA_DIR = Path(os.environ.get("OCR_DATA_DIR", "/var/lib/ocr"))
DB_PATH = DATA_DIR / "ocr.db"
PUBLIC_URL = os.environ.get("OCR_PUBLIC_URL", "https://localhost").rstrip("/")   # what goes in emailed links
API_KEY = os.environ.get("OCR_API_KEY", "")                                       # shared with BC (isolated storage)
ADMINS = {e.strip().lower() for e in os.environ.get("OCR_ADMINS", "").split(",") if e.strip()}
SOURCE_URL = os.environ.get("OCR_SOURCE_URL", "https://github.com/Midstream-Integrations/bc-card-statement-ocr-server")   # AGPL section 13: where portal users get the source
DEV_USER = os.environ.get("OCR_DEV_USER", "")   # local testing only: skips M365 sign-in. NEVER set in production.

# ---- Business Central (service-to-service)
BC_TENANT_ID = os.environ.get("BC_TENANT_ID", "")
BC_ENVIRONMENT = os.environ.get("BC_ENVIRONMENT", "Production")
BC_CLIENT_ID = os.environ.get("BC_CLIENT_ID", "")
BC_CLIENT_SECRET = os.environ.get("BC_CLIENT_SECRET", "")
BC_DIM1_CODE = os.environ.get("BC_DIM1_CODE", "")   # General Ledger Setup -> Shortcut Dimension 1 Code
BC_DIM2_CODE = os.environ.get("BC_DIM2_CODE", "")   # General Ledger Setup -> Shortcut Dimension 2 Code

# ---- card statements
STATEMENT_DIR = DATA_DIR / "statements"
RECEIPT_DIR = DATA_DIR / "receipts"
RECEIPT_DATE_WINDOW = int(os.environ.get("OCR_RECEIPT_DATE_WINDOW", "3"))
# Read each receipt and check it against the charge amount and date. Off: receipts are just attached.
READ_RECEIPTS = os.environ.get("OCR_READ_RECEIPTS", "false").strip().lower() in ("1", "true", "yes", "on")
OCR_TIMEOUT = float(os.environ.get("OCR_TIMEOUT_SECONDS", "5"))   # stop reading a receipt after this many seconds

for d in (DATA_DIR, STATEMENT_DIR, RECEIPT_DIR):
    d.mkdir(parents=True, exist_ok=True)
