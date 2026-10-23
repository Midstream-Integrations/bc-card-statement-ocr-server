"""
Requisition approval <-> Business Central.

Stage 1 (Requisition Manager) Submit:
  G/L lines                 -> one Account line per G/L - the approver's amounts, or the total split evenly
                               (leftover cent on the first line) - qty -1 for a credit, Shortcut Dimension 1/2
                               dimensions from each G/L string, a comment line under each. A G/L the
                               approver didn't know goes in as an uncoded line for Accounts Payable to code
  Requisition Form          -> Cost = total; Description = the approver's explanation (optional);
                               G/L Account = the G/L, or MULTIPLE with the split to the right of the G/L line
  RequisitionForm-<no>      -> stamp Cost, G/L, and the signature
                              ("Approved By:", or "Signed By:" when a 2nd approval is pending)
                            -> upload RequisitionForm-<no>-signed.pdf
  Mgr. Requisition Approval -> APPROVED
  2nd approval pending      -> portal link emailed to the 2nd approver, 2nd Requisition Approval -> APPROVAL SENT
Stage 2 (2nd approver) Submit:
  RequisitionForm-<no>-signed -> stamp "Approved By:" -> upload RequisitionForm-<no>-approved.pdf
  2nd Requisition Approval    -> APPROVED
Every step is recorded, so pressing Submit again after a failure only redoes what didn't finish.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone

from core.bc_client import BCClient, BCError, add_gl_line, attach, finish_gl_line, log
from core.reqform import APPROVED_BY, SIGNED_BY, _download, _money, _stem, stamp_requisition_form
from core.settings import BC_DIM1_CODE, BC_DIM2_CODE, PUBLIC_URL
from reqapproval import mailer, store as db

REQ_PY_VERSION = "2026-10-16a"                          # bump on each change; logged on every submit
GL_FORMAT = re.compile(r"^(\d{5})-(\d{2})-(\d{4})$")    # xxxxx-yy-zzzz
NOT_SENT, SENT, APPROVED = "APPROVAL NOT SENT", "APPROVAL SENT", "APPROVED"


MAX_SPLITS = 10
UNKNOWN = "UNKNOWN"            # the approver ticked "Don't know the G/L"
UNKNOWN_NOTE = "G/L not known - Accounts Payable to code this line."
MAX_DESCRIPTION = 300          # three lines on the Requisition Form


def split_amount(total: float, n: int) -> list[float]:
    """Divide total into n amounts that add back up to it exactly; leftover cents go on the first lines."""
    cents = round(abs(total) * 100)
    base, extra = divmod(cents, n)
    sign = -1 if total < 0 else 1
    return [sign * (base + (1 if i < extra else 0)) / 100 for i in range(n)]


def add_uncoded_line(bc: BCClient, lines_url: str, desc: str, amount: float) -> str:
    """A line with the amount but no G/L account (qty -1 for a credit). If BC won't take an Account
    line without an account, a comment line carries the amount instead."""
    qty, cost = (-1 if amount < 0 else 1), round(abs(amount), 2)
    try:
        line = bc.call("POST", lines_url, f"Add uncoded line '{desc}'", json={
            "lineType": "Account", "description": desc[:100], "quantity": qty, "unitCost": cost})
    except BCError:
        line = bc.call("POST", lines_url, f"Add uncoded line '{desc}'", json={
            "lineType": "Comment", "description": f"UNCODED ${cost:,.2f} {desc}"[:100]})
    return line["id"]


def _lines_of(text: str, size: int = 100):
    """Comment lines of at most 100 characters, broken between words."""
    words, line = text.split(), ""
    for w in words:
        if line and len(line) + 1 + len(w) > 100:
            yield line
            line = w[:100]
        else:
            line = f"{line} {w}" if line else w[:100]
    if line:
        yield line


def add_note(bc: BCClient, lines_url: str, text: str) -> None:
    for part in _lines_of(text):
        bc.call("POST", lines_url, "Add note", json={"lineType": "Comment", "description": part})


def check_split(total: float, gls: list[str], amounts: list[float] | None) -> list[float]:
    """The amount for each G/L: the approver's (must add up to the total exactly), or an even split.
    Raises ValueError with a message for the approver."""
    if not 1 <= len(gls) <= MAX_SPLITS or any(g != UNKNOWN and not GL_FORMAT.match(g) for g in gls):
        raise ValueError("Enter each G/L as xxxxx-yy-zzzz, or tick \"Don't know the G/L\".")
    if not amounts:
        return split_amount(total, len(gls))
    if len(amounts) != len(gls) or any(a is None or a <= 0 for a in amounts):
        raise ValueError("Enter an amount above zero for every G/L, or leave them all blank to split evenly.")
    sign = -1 if total < 0 else 1
    parts = [sign * round(abs(a), 2) for a in amounts]
    diff = round(abs(total) - sum(abs(p) for p in parts), 2)
    if diff:
        raise ValueError(f"The G/L amounts add up to ${sum(abs(p) for p in parts):,.2f}, not ${abs(total):,.2f} "
                         f"({'$%.2f left to assign' % diff if diff > 0 else '$%.2f too much' % -diff}).")
    return parts


def load_splits(req) -> list[dict]:
    try:
        return json.loads(req["splits"]) if req["splits"] else []
    except (TypeError, ValueError):
        return []


def portal_url(token: str) -> str:
    return f"{PUBLIC_URL}/?r={token}"


# ------------------------------------------------------------------ reads
def inv_url(bc: BCClient, invoice_id: str) -> str:
    return f"{bc.std}/purchaseInvoices({invoice_id})"


def requisition_record(bc: BCClient, invoice_id: str) -> dict:
    return bc.call("GET", f"{bc.req}/RequisitionApprovalInvoices({invoice_id})", "Read requisition status")


def attachments(bc: BCClient, invoice_id: str) -> list[dict]:
    return bc.call("GET", f"{inv_url(bc, invoice_id)}/documentAttachments", "Read invoice attachments").get("value", [])


def download(bc: BCClient, invoice_id: str, attachment_id: str) -> bytes:
    return _download(bc, f"{inv_url(bc, invoice_id)}/documentAttachments({attachment_id})/attachmentContent")


def find_form(atts: list[dict], invoice_no: str, which: str) -> dict | None:
    """which: '' (blank form), 'signed' (1st approver) or 'approved' (2nd approver). Newest wins."""
    stem = f"RequisitionForm-{invoice_no}" + (f"-{which}" if which else "")
    hits = [a for a in atts if _stem(a.get("fileName", "")) == stem]
    if not hits and which == "signed":    # signed the old way (emailed back, attached by AP)
        hits = [a for a in atts if "requisition" in a.get("fileName", "").lower()
                and "signed" in a.get("fileName", "").lower()]
    return hits[-1] if hits else None


def vendor_invoice(atts: list[dict]) -> dict | None:
    """Earliest attachment that isn't a requisition form."""
    cand = [a for a in atts if "requisition" not in a.get("fileName", "").lower()]
    cand.sort(key=lambda a: a.get("lastModifiedDateTime") or a.get("createdDateTime") or "")
    return cand[0] if cand else None


def second_pending(rec: dict) -> bool:
    return rec.get("HigherRequisitionApproval") == NOT_SENT and bool((rec.get("HigherRequisitionEmail") or "").strip())


# ------------------------------------------------------------------ writes
def _put_form(bc, invoice_id, invoice_no, source: str, target: str, label: str,
              signed_by: str, signed_at: datetime, cost=None, gl="", description=None, gl_splits=None,
              on_behalf: str | None = None) -> None:
    atts = attachments(bc, invoice_id)
    if any(_stem(a.get("fileName", "")) == f"RequisitionForm-{invoice_no}-{target}" for a in atts):
        log.info("RequisitionForm-%s-%s already on the invoice", invoice_no, target)
        return
    src = find_form(atts, invoice_no, source)
    if not src:
        name = f"RequisitionForm-{invoice_no}" + (f"-{source}" if source else "")
        raise BCError(f"{name} isn't attached to invoice {invoice_no} - ask Accounts Payable to re-send it.")
    try:
        stamped = stamp_requisition_form(download(bc, invoice_id, src["id"]), cost, gl, signed_by, signed_at,
                                         sign_label=label, description=description, gl_splits=gl_splits,
                                         on_behalf=on_behalf)
    except ValueError as e:
        raise BCError(f"Sign requisition form: {e}") from e
    attach(bc, inv_url(bc, invoice_id), f"RequisitionForm-{invoice_no}-{target}.pdf", stamped, "signed requisition form")


def _set_status(bc, invoice_id, field: str, value: str) -> None:
    bc.call("PATCH", f"{bc.req}/RequisitionApprovalInvoices({invoice_id})", f"Set {field} = {value}",
            json={field: value}, headers={"If-Match": "*"})


def submit(req, amount: float | None, gls: list[str], signed_by: str, signed_at: datetime,
           description: str = "", amounts: list[float] | None = None, on_behalf: str | None = None) -> str:
    """gls: one G/L string, or several to split the amount across (UNKNOWN for one the approver doesn't
    know). amounts: the approver's amount per G/L, or None to split evenly. description: optional
    explanation for the Requisition Form. on_behalf: the approver's name when a delegate signs for
    them. Returns a message for the approver; raises BCError / ValueError on anything they need to fix."""
    who = f"{signed_by} on behalf of {on_behalf}" if on_behalf else signed_by
    req = db.get(req["token"])                          # fresh flags: never redo finished steps
    tok, inv_id, no = req["token"], req["invoice_id"], req["invoice_no"]
    log.info("reqapproval submit %s invoice %s stage %s by %s req.py %s", tok[:8], no, req["stage"], signed_by, REQ_PY_VERSION)
    bc = BCClient(req["company_id"])
    inv = bc.call("GET", inv_url(bc, inv_id), "Open purchase invoice")
    if inv.get("status") not in (None, "Draft", "Open"):
        raise BCError(f"Purchase invoice {no} is {inv.get('status')}, not open.")

    if req["stage"] == 2:
        if not req["form_done"]:
            _put_form(bc, inv_id, no, "signed", "approved", APPROVED_BY, signed_by, signed_at, on_behalf=on_behalf)
            db.update(tok, form_done=1)
        if not req["approval_done"]:
            _set_status(bc, inv_id, "HigherRequisitionApproval", APPROVED)
            db.update(tok, approval_done=1)
        db.mark_submitted(tok, signed_by, signed_at.isoformat())
        return f"Approved. The signed Requisition Form is on invoice {no}."

    # ---- stage 1
    if not (BC_DIM1_CODE and BC_DIM2_CODE):
        raise BCError("BC_DIM1_CODE / BC_DIM2_CODE are not set in config/ocr.env")
    splits = load_splits(req)
    if not splits and req["bc_line_id"]:                 # line added by the 2026-10-09b version
        splits = [{"gl": req["gl"], "amount": req["amount"], "line_id": req["bc_line_id"], "done": req["bc_line_done"]}]
    if any(x.get("line_id") for x in splits):           # lines already on the invoice: keep that split
        log.info("reqapproval %s: resuming with the saved split", no)
    else:
        parts = check_split(amount, gls, amounts)
        bad = sorted({g.split("-")[0] for g in gls if g != UNKNOWN and not bc.account_ok(g.split("-")[0])})
        if bad:
            raise ValueError(f"G/L account {', '.join(bad)} isn't an open posting account in Business Central. "
                             "Correct it, or tick \"Don't know the G/L\".")
        splits = [{"gl": g, "amount": a} for g, a in zip(gls, parts)]
        db.update(tok, splits=json.dumps(splits))
    total = round(sum(x["amount"] for x in splits), 2)
    gl_text = splits[0]["gl"] if len(splits) == 1 else "MULTIPLE"
    rec = requisition_record(bc, inv_id)
    two = second_pending(rec)

    # the G/L lines (each recorded as soon as BC accepts it, so a retry never duplicates one)
    lines_url = f"{inv_url(bc, inv_id)}/purchaseInvoiceLines"
    base = " - ".join(x for x in (req["vendor_invoice_no"], req["vendor_name"]) if x) or f"Invoice {no}"
    for i, x in enumerate(splits, start=1):
        known = x["gl"] != UNKNOWN
        suffix = f" ({i}/{len(splits)})" if len(splits) > 1 else ""
        desc = base[:100 - len(suffix)] + suffix
        note = f"Amount and G/L by {who} via OCR portal" + (
            f" - split {i} of {len(splits)} of {_money(total)}" if len(splits) > 1 else "")
        if known:
            account, dim1, dim2 = GL_FORMAT.match(x["gl"]).groups()
        if not x.get("line_id"):
            x["line_id"] = (add_gl_line(bc, lines_url, desc, x["amount"], account)[0] if known
                            else add_uncoded_line(bc, lines_url, desc, x["amount"]))
            db.update(tok, splits=json.dumps(splits))
        if not x.get("done"):
            if known:
                finish_gl_line(bc, lines_url, x["line_id"], desc, x["amount"], account, bc.account_ok(account),
                               [(BC_DIM1_CODE, dim1), (BC_DIM2_CODE, dim2)], extra_note=note)
            else:
                add_note(bc, lines_url, f"{UNKNOWN_NOTE} {note}")
            x["done"] = 1
            db.update(tok, splits=json.dumps(splits))

    if not req["form_done"]:
        description = (description or "").strip()[:MAX_DESCRIPTION]
        gl_splits = [f"{x['gl']}  {_money(x['amount'])}" for x in splits] if len(splits) > 1 else None
        _put_form(bc, inv_id, no, "", "signed", SIGNED_BY if two else APPROVED_BY,
                  signed_by, signed_at, cost=total, gl=gl_text, description=description, gl_splits=gl_splits,
                  on_behalf=on_behalf)
        db.update(tok, form_done=1, amount=total, gl=gl_text, description=description)
    if not req["approval_done"]:
        _set_status(bc, inv_id, "mgrRequisitionApproval", APPROVED)
        db.update(tok, approval_done=1)
    db.mark_submitted(tok, signed_by, signed_at.isoformat())
    log.info("reqapproval %s: cost %s G/L %s signed by %s (2nd approval: %s)", no, _money(total),
             ", ".join(f"{x['gl']} {_money(x['amount'])}" for x in splits), signed_by, two)

    if not two:
        return f"Approved. The signed Requisition Form is on invoice {no}."
    try:
        send_second(bc, rec, req["company_id"], first=db.get(tok), signer=who)
        db.update(tok, second_done=1)
        return (f"Signed. Invoice {no} has gone to {rec.get('HigherRequisitionName') or rec.get('HigherRequisitionEmail')} "
                "for the 2nd approval.")
    except Exception as e:                              # your part is done; the sweep retries the email
        log.error("2nd approval email for %s failed: %s", no, e)
        return (f"Signed. The signed Requisition Form is on invoice {no}. The email to the 2nd approver "
                "didn't go out yet - it will be retried automatically.")


def send_second(bc: BCClient, rec: dict, company_id: str, first=None, *, source: str = "requisition",
                attachment: tuple[str, str] | None = None, signer: str | None = None,
                amount: float | None = None, gl: str | None = None) -> str:
    """Create the 2nd approver's portal link, email it, set 2nd Requisition Approval = APPROVAL SENT.
    rec: the RequisitionApprovalInvoices record. first: the submitted stage-1 row, when there is one.
    Card statements pass source="card", the statement attachment (id, name), and the 1st approver's
    email / total / G/L directly, since they have no stage-1 row here."""
    inv_id = (rec.get("Guid") or rec.get("id") or "").strip("{}").lower()
    no = rec.get("InvoiceNo") or (first["invoice_no"] if first else "")
    email = (rec.get("HigherRequisitionEmail") or "").strip()
    existing = db.find_open(inv_id, 2)
    if existing and existing["emailed_at"] and existing["approver_email"] == email.lower():
        log.info("2nd approval for %s already emailed", no)
    else:
        if existing and existing["approver_email"] == email.lower():
            token = existing["token"]
        else:
            if attachment:
                att_id, att_name = attachment
            elif first:
                att_id, att_name = first["attachment_id"], first["attachment_name"]
            else:
                inv = vendor_invoice(attachments(bc, inv_id))
                att_id, att_name = (inv["id"], inv.get("fileName", "")) if inv else ("", "")
            token = secrets.token_urlsafe(24)
            db.create(token=token, stage=2, invoice_id=inv_id, invoice_no=no,
                      company_id=company_id,
                      vendor_no=rec.get("VendorNo"), vendor_name=rec.get("VendorName"),
                      vendor_invoice_no=rec.get("VendorInvoiceNo"), attachment_id=att_id, attachment_name=att_name,
                      approver_email=email, approver_name=rec.get("HigherRequisitionName"),
                      creator_email=rec.get("RequisitionCreatorEmail"),
                      amount=first["amount"] if first else amount, gl=first["gl"] if first else gl,
                      splits=first["splits"] if first else None,
                      description=first["description"] if first else None,
                      first_signer=signer or (first["submitted_by"] if first else None), source=source)
        link = portal_url(token)
        what = "card statement" if source == "card" else "Invoice"
        text = (
            f"Please review the {what} and the Requisition Form and approve it at the link below. "
            "Press Approve to e-sign the Requisition Form.\n\n"
            f"{link}\n\n"
            "Sign in with your company Microsoft 365 account.\n\n"
            f"Invoice Number - {no}\n"
            f"Vendor Number - {rec.get('VendorNo') or ''}\n"
            f"Vendor Invoice Number - {rec.get('VendorInvoiceNo') or ''}\n\n" + mailer.FOOTER)
        mailer.send(email, f"Secondary Requisition Approval Required: {rec.get('VendorName')} -  Invoice #: {no}",
                    text, link, cc=[rec.get("RequisitionCreatorEmail") or ""])
        db.update(token, emailed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        log.info("emailed 2nd approver %s for %s", email, no)
    _set_status(bc, inv_id, "HigherRequisitionApproval", SENT)
    return email
