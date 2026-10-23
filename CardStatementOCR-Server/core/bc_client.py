"""
Business Central client shared by every module: token, retries on throttling, lookups,
line dimensions and document attachments. 
"""
from __future__ import annotations

import logging
import time

import requests

from core.settings import BC_CLIENT_ID, BC_CLIENT_SECRET, BC_ENVIRONMENT, BC_TENANT_ID

log = logging.getLogger("ocr.bc")          # -> journalctl -u ocr-portal
if not log.handlers:                         # own handler: Streamlit's logging setup can swallow ours
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s ocr.bc %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

API_ROOT = "https://api.businesscentral.dynamics.com/v2.0"


class BCError(Exception):
    pass


def _token() -> str:
    resp = requests.post(
        f"https://login.microsoftonline.com/{BC_TENANT_ID}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials", "client_id": BC_CLIENT_ID,
              "client_secret": BC_CLIENT_SECRET,
              "scope": "https://api.businesscentral.dynamics.com/.default"},
        timeout=30)
    if resp.status_code != 200:
        raise BCError(f"Failed to obtain BC token: {resp.status_code} {resp.text[:300]}")
    return resp.json()["access_token"]


def _msg(resp: requests.Response) -> str:
    try:
        return resp.json()["error"]["message"]
    except Exception:
        return resp.text[:500]


class BCClient:
    def __init__(self, company_id: str):
        self.token = _token()
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {self.token}",
                               "Content-Type": "application/json", "Accept": "application/json"})
        root = f"{API_ROOT}/{BC_TENANT_ID}/{BC_ENVIRONMENT}"
        self.std = f"{root}/api/v2.0/companies({company_id})"
        self.req = f"{root}/api/midstream/RequisitionApprovalInvoices/v1.0/companies({company_id})"
        self._accounts: dict[str, bool] = {}
        self._dims: dict[str, str] = {}

    def call(self, method: str, url: str, what: str, **kw) -> dict:
        for attempt in range(6):                       # BC throttles with 429 / 503: wait and retry
            resp = self.s.request(method, url, timeout=120, **kw)
            if resp.status_code not in (429, 503):
                break
            wait = float(resp.headers.get("Retry-After") or 0) or min(2 ** attempt, 30)
            log.info("BC throttled %s (%s); retrying in %.0fs", what, resp.status_code, wait)
            time.sleep(wait)
        if resp.status_code not in (200, 201, 204):
            raise BCError(f"{what}: {resp.status_code} {_msg(resp)}")
        return resp.json() if resp.content else {}

    # ---- lookups: anything that doesn't exist in BC is left blank on the line
    def account_ok(self, number: str) -> bool:
        if number not in self._accounts:
            rows = self.call("GET", f"{self.std}/accounts", "Look up G/L account",
                             params={"$filter": f"number eq '{number}'"}).get("value", [])
            a = rows[0] if rows else None
            self._accounts[number] = bool(a) and not a.get("blocked", False) \
                and a.get("accountType", "Posting") == "Posting" and a.get("directPosting", True)
        return self._accounts[number]

    def dim_id(self, dim_code: str) -> str:
        if dim_code not in self._dims:
            rows = self.call("GET", f"{self.std}/dimensions", f"Look up dimension {dim_code}",
                             params={"$filter": f"code eq '{dim_code}'"}).get("value", [])
            if not rows:
                raise BCError(f"Dimension '{dim_code}' doesn't exist in BC - check BC_DIM1_CODE / BC_DIM2_CODE "
                              "in config/ocr.env against General Ledger Setup.")
            self._dims[dim_code] = rows[0]["id"]
        return self._dims[dim_code]

    def set_line_dim(self, line_url: str, dim_code: str, value: str, desc: str) -> bool:
        """Put dim_code=value on the line, sending only the call that applies:
        PATCH if the account's default already put the dimension there, otherwise POST.
        (Deliberately failing calls get BC to throttle us with 429.)
        If BC rejects the value, the dimension is cleared so the field is left blank."""
        dims = f"{line_url}/dimensionSetLines"
        current = {d["code"]: d.get("valueCode") for d in self.call("GET", dims, f"Read dimensions on '{desc}'").get("value", [])}
        one = f"{dims}({self.dim_id(dim_code)})"
        if current.get(dim_code) == value:
            log.info("[%s] %s already %s", desc, dim_code, value)
            return True
        try:
            if dim_code in current:
                self.call("PATCH", one, f"Update {dim_code} on '{desc}'", json={"valueCode": value}, headers={"If-Match": "*"})
            else:
                self.call("POST", dims, f"Set {dim_code} on '{desc}'", json={"code": dim_code, "valueCode": value})
            log.info("[%s] %s set to %s", desc, dim_code, value)
            return True
        except BCError as e:
            log.warning("[%s] %s=%s rejected by BC: %s", desc, dim_code, value, e)
        if dim_code in current:          # value doesn't exist in BC -> leave the field blank (remove the default)
            try:
                self.call("DELETE", one, f"Clear {dim_code} on '{desc}'", headers={"If-Match": "*"})
            except BCError as e:
                log.warning("[%s] clear %s failed: %s", desc, dim_code, e)
        return False


def attach(bc: "BCClient", inv_url: str, name: str, data: bytes, what: str) -> str:
    """Create the documentAttachment, then PATCH its content."""
    att = bc.call("POST", f"{inv_url}/documentAttachments", f"Attach {what}", json={"fileName": name[:250]})
    link = att.get("attachmentContent@odata.mediaEditLink")
    if not link:
        raise BCError(f"No media edit link returned for {name}; cannot upload content.")
    for attempt in range(6):
        resp = requests.patch(link, data=data, timeout=180,
                              headers={"Authorization": f"Bearer {bc.token}",
                                       "Content-Type": "application/octet-stream", "If-Match": "*"})
        if resp.status_code not in (429, 503):
            break
        time.sleep(float(resp.headers.get("Retry-After") or 0) or min(2 ** attempt, 30))
    if resp.status_code not in (200, 204):
        raise BCError(f"Upload {what} ({name}): {resp.status_code} {_msg(resp)}")
    return att["id"]




def chunks(text: str, size: int = 100):
    while text:
        yield text[:size]
        text = text[size:]


def add_gl_line(bc: "BCClient", lines_url: str, desc: str, amount: float, account: str) -> tuple[str, bool]:
    """Create one G/L line (qty 1, or -1 for a credit). An account BC doesn't have is left blank.
    Returns (line id, account_ok)."""
    qty, cost = (-1 if amount < 0 else 1), round(abs(amount), 2)
    if bc.account_ok(account):
        line = bc.call("POST", lines_url, f"Add line '{desc}'", json={
            "lineType": "Account", "lineObjectNumber": account, "description": desc[:100],
            "quantity": qty, "unitCost": cost})
        return line["id"], True
    try:
        line = bc.call("POST", lines_url, f"Add line '{desc}'", json={
            "lineType": "Account", "description": desc[:100], "quantity": qty, "unitCost": cost})
    except BCError:
        line = bc.call("POST", lines_url, f"Add line '{desc}'", json={
            "lineType": "Comment", "description": f"UNCODED ${cost:,.2f} {desc}"[:100]})
    return line["id"], False


def finish_gl_line(bc: "BCClient", lines_url: str, line_id: str, desc: str, amount: float, account: str,
                   account_ok: bool, dims: list[tuple[str, str]], extra_note: str = ""):
    """Dimensions + a comment line with anything that was left blank (and any extra note)."""
    notes = [] if account_ok else [f"G/L {account} not found - left blank. Amount ${abs(amount):,.2f}."]
    if account_ok:
        for code, value in dims:
            if value and not bc.set_line_dim(f"{lines_url}({line_id})", code, value, desc):
                notes.append(f"{code} {value} not found - left blank.")
    for part in chunks(" ".join(x for x in [extra_note, *notes] if x)):
        bc.call("POST", lines_url, f"Add note for '{desc}'", json={"lineType": "Comment", "description": part})
