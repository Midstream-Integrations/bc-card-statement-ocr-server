"""
Requisition approval: endpoints BC's Send for Requisition Approval calls.

  POST /api/reqapproval/send              JSON from BC's Send for Requisition Approval -> checks the invoice,
                                          creates the portal link and emails the Requisition Manager
  POST /api/reqapproval/requests          JSON -> token + portal URL (link only, no email)
  GET  /api/reqapproval/requests/{token}  -> status (open / submitted / superseded)

Header X-API-Key, same key as the card-statement endpoints.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from core.auth import require_key
from core.bc_client import BCClient, BCError, log
from reqapproval import mailer, store as db
from reqapproval.bc import attachments, find_form, portal_url, requisition_record, second_pending

router = APIRouter(dependencies=[Depends(require_key)])


class RequestIn(BaseModel):
    invoiceId: str              # Purchase Header SystemId
    invoiceNo: str
    companyId: str
    vendorNo: str = ""
    vendorName: str = ""
    vendorInvoiceNo: str = ""
    attachmentId: str           # the vendor invoice PDF the approver will see
    attachmentName: str = ""
    managerEmail: EmailStr
    managerName: str = ""
    creatorEmail: str = ""


def _open_request(r: RequestIn, replace: bool = True) -> tuple[str, bool]:
    """Stage-1 portal link for this invoice: the same one again if BC retries, else a new one.
    Returns (token, created a new one)."""
    invoice_id = r.invoiceId.strip("{}").lower()
    attachment_id = r.attachmentId.strip("{}").lower()
    existing = db.find_open(invoice_id, 1)
    if existing and existing["approver_email"] == str(r.managerEmail).lower() \
            and existing["attachment_id"] == attachment_id:
        return existing["token"], False         # same send retried: same link
    else:
        token = secrets.token_urlsafe(24)       # new send replaces the older open link
        db.create(replace=replace, token=token, stage=1, invoice_id=invoice_id, invoice_no=r.invoiceNo,
                  company_id=r.companyId.strip("{}").lower(), vendor_no=r.vendorNo, vendor_name=r.vendorName,
                  vendor_invoice_no=r.vendorInvoiceNo, attachment_id=attachment_id,
                  attachment_name=r.attachmentName, approver_email=str(r.managerEmail),
                  approver_name=r.managerName, creator_email=r.creatorEmail)
    return token, True


@router.post("/api/reqapproval/requests")
def create_request(r: RequestIn):
    token, _ = _open_request(r)
    return {"token": token, "url": portal_url(token)}


@router.post("/api/reqapproval/send")
def send_request(r: RequestIn):
    """Called by BC's Send for Requisition Approval: check the invoice, create the link, email the manager.
    Any failure comes back as an error, so BC undoes the attachment and Requisition Form it saved."""
    invoice_id = r.invoiceId.strip("{}").lower()
    try:
        bc = BCClient(r.companyId.strip("{}").lower())
        rec = requisition_record(bc, invoice_id)
        if not find_form(attachments(bc, invoice_id), r.invoiceNo, ""):
            raise HTTPException(422, f"RequisitionForm-{r.invoiceNo} isn't attached to the invoice.")
    except BCError as e:
        raise HTTPException(502, f"Business Central: {e}")

    token, new = _open_request(r, replace=False)   # older links stay live until this email goes out
    link = portal_url(token)
    second = (rec.get("HigherRequisitionName") or rec.get("HigherRequisitionEmail") or "") if second_pending(rec) else ""
    text = (
        "Please review the Invoice and approve the Requisition at the link below. "
        "Enter the invoice amount to pay and the G/L account, then press Submit - "
        "the Requisition Form is e-signed for you.\n\n"
        f"{link}\n\n"
        "Sign in with your company Microsoft 365 account.\n\n"
        f"Invoice Number - {r.invoiceNo}\n"
        f"Vendor Number - {r.vendorNo}\n"
        f"Vendor Invoice Number - {r.vendorInvoiceNo}\n"
        + (f"2nd approval required by - {second} (to be handled by the system)\n" if second else "")
        + "\n" + mailer.FOOTER)
    try:
        mailer.send(str(r.managerEmail), f"Requisition Approval Required: {r.vendorName} - Invoice #{r.attachmentName}",
                    text, link, cc=[r.creatorEmail])
    except Exception as e:
        log.error("requisition approval email for %s failed: %s", r.invoiceNo, e)
        if new:
            db.discard(token)            # BC undoes the send - nobody has this link
        raise HTTPException(502, f"The approval email could not be sent: {e}")
    db.activate(token)                   # now the new link replaces any older one for this invoice
    db.update(token, emailed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    log.info("requisition approval for %s emailed to %s (2nd approver: %s)", r.invoiceNo, r.managerEmail, second or "none")
    return {"token": token, "url": link, "secondApprover": second}


@router.get("/api/reqapproval/requests/{token}")
def request_status(token: str):
    req = db.get(token)
    if not req:
        raise HTTPException(404, "Unknown token")
    return {"status": req["status"], "stage": req["stage"],
            "submittedAt": req["submitted_at"], "submittedBy": req["submitted_by"]}
