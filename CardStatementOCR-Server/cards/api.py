"""
Card statements: BC-facing endpoints (mounted by api.py; nginx routes https://<host>/api/* there).

  GET  /api/merchants                         -> issuers this server can parse
  POST /api/statements?merchant=auto          body: raw PDF -> statementId + cardholder list
  POST /api/assignments                       JSON -> token + portal URL to email to the manager
  GET  /api/assignments/{token}               -> status (open / submitted)

Every call needs header X-API-Key (stored in BC isolated storage).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr

from cards import store as db
from cards.parsers import MERCHANTS, parse_statement, reconcile
from core.auth import require_key
from core.settings import PUBLIC_URL, STATEMENT_DIR

# Paths are unchanged from the original single-module API, so BC needs no changes.
router = APIRouter(dependencies=[Depends(require_key)])


@router.get("/api/merchants")
def merchants():
    return [{"id": m.id, "label": m.label} for m in MERCHANTS.values()]


@router.post("/api/statements")
async def upload_statement(request: Request, merchant: str = "auto", filename: str = "statement.pdf"):
    pdf = await request.body()
    if not pdf.startswith(b"%PDF"):
        raise HTTPException(415, "Body must be a PDF file")
    try:
        st = parse_statement(pdf, merchant)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if st.cardholders.empty:
        raise HTTPException(422, "; ".join(st.warnings) or "No cardholders found")

    sha = hashlib.sha256(pdf).hexdigest()
    existing = db.find_statement_by_sha(sha, st.merchant)
    if existing:                      # same PDF again -> reuse it, so re-sends return the same portal link
        sid = existing["id"]
    else:
        sid = uuid.uuid4().hex
        path = STATEMENT_DIR / f"{sid}.pdf"
        path.write_bytes(pdf)
        db.save_statement(sid, st, re.sub(r"[^\w.\- ]", "_", filename)[:200], path, sha)

    rec = reconcile(st)
    counts = st.transactions[st.transactions["type"] != "Payment"].groupby("last4").size()
    holders = []
    for r in rec.itertuples():
        n = int(counts.get(r.last4, 0))
        if n == 0:                      # nothing to document (e.g. admin card, unused cards)
            continue
        total = r.parsed_purchases
        flag = " (CHECK - total does not match statement)" if r.matches is False else ""
        holders.append({
            "name": r.name, "last4": r.last4, "role": r.role, "transactions": n,
            "total": total, "reconciled": r.matches is not False,
            # BC shows these in a StrMenu, which splits on commas -> no commas allowed
            "label": f"{r.name.replace(',', ' ')} - ...{r.last4} - ${total:.2f} ({n} txns){flag}",
        })
    return {"statementId": sid, "merchant": st.merchant, "closingDate": str(st.closing_date or ""),
            "method": st.method, "warnings": st.warnings, "cardholders": holders}


class AssignmentIn(BaseModel):
    statementId: str
    last4: str
    invoiceId: str          # Purchase Header SystemId
    invoiceNo: str
    companyId: str          # BC company id (CompanyProperty.ID())
    managerEmail: EmailStr
    managerName: str = ""
    senderEmail: str = ""   # the BC user who sent it; emailed when the manager approves


@router.post("/api/assignments")
def create_assignment(a: AssignmentIn):
    stmt = db.get_statement(a.statementId)
    if not stmt:
        raise HTTPException(404, "Unknown statementId")
    invoice_id = a.invoiceId.strip("{}").lower()
    company_id = a.companyId.strip("{}").lower()

    existing = db.find_open_assignment(a.statementId, a.last4, invoice_id)
    if existing:                                   # BC retried: hand back the same link
        token = existing["token"]
    else:
        with db.conn() as c:
            row = c.execute("SELECT name FROM transactions WHERE statement_id=? AND last4=? LIMIT 1",
                            (a.statementId, a.last4)).fetchone()
        if not row:
            raise HTTPException(422, f"No transactions for card ...{a.last4} on this statement")
        token = secrets.token_urlsafe(24)
        db.create_assignment(token=token, statement_id=a.statementId, last4=a.last4, cardholder=row["name"],
                             invoice_id=invoice_id, invoice_no=a.invoiceNo, company_id=company_id,
                             manager_email=str(a.managerEmail), manager_name=a.managerName,
                             sender_email=a.senderEmail)
    return {"token": token, "url": f"{PUBLIC_URL}/?t={token}"}


@router.get("/api/assignments/{token}")
def assignment_status(token: str):
    a = db.get_assignment(token)
    if not a:
        raise HTTPException(404, "Unknown token")
    return {"status": a["status"], "submittedAt": a["submitted_at"], "submittedBy": a["submitted_by"]}
