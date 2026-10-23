"""
Push a finished card-statement assignment back to the BC purchase invoice:
cardholder header block, G/L line(s) per charge (+ dimensions, notes, receipt),
the signed Requisition Form, then Mgr. Requisition Approval = APPROVED.
Every step is recorded so a resubmit never duplicates.
(The statement PDF itself is attached by BC when the statement is sent.)

A charge split across several G/L accounts becomes one line per G/L: the user's amount for each,
or the charge divided evenly (leftover cent on the first line), "(1/3)" etc. on the descriptions.
A G/L the user didn't know ("Don't know the G/L") goes in as an uncoded line for AP to code. The user's description of
the charge goes in the comment under its (first) line. The Requisition Form gets only Cost, G/L
(the one string, or MULTIPLE) and the signature - no split, no descriptions.

2nd approver (picked in BC when the statement is sent): the cardholder's manager signs "Signed By:"
instead of "Approved By:", and the 2nd approver is emailed a Requisition Approval portal link showing
only the card statement and the signed Requisition Form (no receipts). Their Approve stamps
"Approved By:" and sets 2nd Requisition Approval = APPROVED.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from cards import store as db
from cards.parsers import MERCHANTS
from core.bc_client import BCClient, BCError, add_gl_line, attach, finish_gl_line, log
from core.reqform import APPROVED_BY, SIGNED_BY, _central, _money, _stem, gl_summary, sign_requisition_form
from core.settings import BC_DIM1_CODE, BC_DIM2_CODE

BC_PY_VERSION = "2026-10-15a"                # bump on each change; logged on every submit
GL_FORMAT = re.compile(r"^(\d{5})-(\d{2})-(\d{4})$")   # numbers and dashes only


def split_amount(total: float, n: int) -> list[float]:
    """Divide total into n amounts that add back up to it exactly; leftover cents go on the first lines."""
    cents = round(abs(total) * 100)
    base, extra = divmod(cents, n)
    sign = -1 if total < 0 else 1
    return [sign * (base + (1 if i < extra else 0)) / 100 for i in range(n)]


UNKNOWN_NOTE = "G/L not known - Accounts Payable to code this line."


def split_for(r, gls: list[str]) -> list[float]:
    """The user's amounts when every one is filled in, else an even split of the charge."""
    amts = db.amounts_of(r)
    if all(a is not None for a in amts) and len(amts) == len(gls):
        sign = -1 if r["amount"] < 0 else 1
        return [sign * round(abs(float(a)), 2) for a in amts]
    return split_amount(r["amount"], len(gls))


def _uncoded_line(bc, lines_url, desc, amount) -> str:
    """A line with the amount but no G/L account; a comment line carries it if BC won't take that."""
    qty, cost = (-1 if amount < 0 else 1), round(abs(amount), 2)
    try:
        line = bc.call("POST", lines_url, f"Add uncoded line '{desc}'", json={
            "lineType": "Account", "description": desc, "quantity": qty, "unitCost": cost})
    except BCError:
        line = bc.call("POST", lines_url, f"Add uncoded line '{desc}'", json={
            "lineType": "Comment", "description": f"UNCODED ${cost:,.2f} {desc}"[:100]})
    return line["id"]


def _receipt_note(r) -> str:
    status, note = r["check_status"] or "", (r["note"] or "").strip()
    if status in ("match", "attached", ""):
        return "No receipt (credit)." if r["type"] == "Credit" and not r["file_path"] else ""
    return {
        "found": "Receipt: amount found but not on a total line.",
        "missing": "Receipt missing.",
        "date_mismatch": f"Receipt date not within a few days of the charge. {note}",
        "unreadable": f"Receipt could not be read automatically. {note}",
    }.get(status, f"Receipt total did not match ({status}). {note}").strip()


def _memo(r) -> str:
    m = " ".join((r["memo"] or "").split())
    if m and m[-1] not in ".!?":
        m += "."
    return f"Description: {m}" if m else ""


def _chunks(text: str, size: int = 100):
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


def _header_lines(assignment, rows, submitted_by: str) -> list[str]:
    stmt = db.get_statement(assignment["statement_id"]) or {}
    info = db.cardholder_info(assignment["statement_id"], assignment["last4"]) or {}
    label = MERCHANTS[stmt["merchant"]].label if stmt.get("merchant") in MERCHANTS else (stmt.get("merchant") or "Card")
    closing = stmt.get("closing_date") or ""
    if len(closing) == 10:
        closing = f"{closing[5:7]}/{closing[8:]}/{closing[:4]}"
    purchases = sum(r["amount"] for r in rows if r["type"] == "Purchase")
    credits = sum(r["amount"] for r in rows if r["type"] != "Purchase")
    limit = info.get("credit_limit")
    total = info.get("statement_total")
    out = [
        f"CARD STATEMENT - {label} - closing {closing}",
        f"Cardholder: {info.get('name') or assignment['cardholder']}  Card ...{assignment['last4']}"
        + (f"  Limit ${limit:,.2f}" if limit is not None else ""),
        f"Statement total ${total if total is not None else purchases:,.2f}  Charges {len(rows)}"
        + (f"  Credits ${credits:,.2f}" if credits else ""),
        f"Receipts and G/L by {submitted_by} via OCR portal",
    ]
    return [x[:100] for x in out]


def _post_header(bc: "BCClient", lines_url: str, assignment, rows, submitted_by: str):
    """Comment lines at the very top of the invoice. Uses line numbers below the first existing line;
    if there's no room (or BC won't take a line number), they go at the end instead."""
    text = _header_lines(assignment, rows, submitted_by)
    existing = [l.get("sequence") or 0 for l in bc.call("GET", lines_url, "Read invoice lines").get("value", [])]
    first = min(existing) if existing else 0
    seqs = [None] * len(text)
    if existing and first > len(text):
        step = first // (len(text) + 1)
        seqs = [step * (i + 1) for i in range(len(text))]
    for part, seq in zip(text, seqs):
        body = {"lineType": "Comment", "description": part}
        if seq:
            body["sequence"] = seq
        try:
            bc.call("POST", lines_url, "Add statement header", json=body)
        except BCError:
            if not seq:
                raise
            body.pop("sequence")                       # BC wouldn't take the line number -> append
            log.warning("header line number refused; adding '%s' at the end", part)
            bc.call("POST", lines_url, "Add statement header", json=body)


def _single_line(bc, lines_url, tok, r, gl_string, desc):
    """One G/L line for the whole charge (unchanged from 2026-10-08a, plus the description)."""
    if gl_string == db.UNKNOWN:
        if not r["bc_line_id"]:
            db.upsert_entry(tok, r["id"], bc_line_id=_uncoded_line(bc, lines_url, desc, r["amount"]))
        if not r["bc_line_done"]:
            text = " ".join(x for x in [_memo(r), _receipt_note(r), UNKNOWN_NOTE] if x)
            for part in _chunks(text):
                bc.call("POST", lines_url, f"Add note for '{desc}'", json={"lineType": "Comment", "description": part})
            db.upsert_entry(tok, r["id"], bc_line_done=1)
        return
    qty = -1 if r["amount"] < 0 else 1
    cost = round(abs(r["amount"]), 2)
    gl, dim1, dim2 = GL_FORMAT.match(gl_string).groups()
    gl_ok = bc.account_ok(gl)
    line_id = r["bc_line_id"]
    if not line_id:
        if gl_ok:
            line = bc.call("POST", lines_url, f"Add line '{desc}'", json={
                "lineType": "Account", "lineObjectNumber": gl, "description": desc,
                "quantity": qty, "unitCost": cost})
        else:
            try:      # account line with no account; BC may refuse a cost without one
                line = bc.call("POST", lines_url, f"Add line '{desc}'", json={
                    "lineType": "Account", "description": desc, "quantity": qty, "unitCost": cost})
            except BCError:
                line = bc.call("POST", lines_url, f"Add line '{desc}'", json={
                    "lineType": "Comment", "description": f"UNCODED ${cost:,.2f} {desc}"[:100]})
        line_id = line["id"]
        db.upsert_entry(tok, r["id"], bc_line_id=line_id)

    if not r["bc_line_done"]:
        log.info("line '%s': G/L %s ok=%s, setting %s=%s %s=%s", desc, gl, gl_ok, BC_DIM1_CODE, dim1, BC_DIM2_CODE, dim2)
        notes = [] if gl_ok else [f"G/L {gl} not found - left blank. Charge ${cost:,.2f}."]
        for code, value in ((BC_DIM1_CODE, dim1), (BC_DIM2_CODE, dim2)):
            if not gl_ok:
                continue                      # no account on the line -> AP codes it
            if not bc.set_line_dim(f"{lines_url}({line_id})", code, value, desc):
                notes.append(f"{code} {value} not found - left blank.")
        text = " ".join(x for x in [_memo(r), _receipt_note(r), *notes] if x)
        for part in _chunks(text):
            bc.call("POST", lines_url, f"Add note for '{desc}'", json={"lineType": "Comment", "description": part})
        db.upsert_entry(tok, r["id"], bc_line_done=1)


def _split_lines(bc, lines_url, tok, r, gls, desc):
    """One G/L line per split. Each is recorded as soon as BC accepts it, so a retry never duplicates."""
    try:
        splits = json.loads(r["bc_splits"]) if r["bc_splits"] else []
    except ValueError:
        splits = []
    if not any(x.get("line_id") for x in splits):          # nothing in BC yet: (re)build from the entries
        splits = [{"gl": g, "amount": a} for g, a in zip(gls, split_for(r, gls))]
        db.upsert_entry(tok, r["id"], bc_splits=json.dumps(splits))
    n, total = len(splits), abs(r["amount"])
    first_note = " ".join(x for x in [_memo(r), _receipt_note(r)] if x)
    for i, x in enumerate(splits, start=1):
        known = x["gl"] != db.UNKNOWN
        suffix = f" ({i}/{n})"
        sdesc = desc[:100 - len(suffix)] + suffix
        if known:
            account, dim1, dim2 = GL_FORMAT.match(x["gl"]).groups()
        if not x.get("line_id"):
            x["line_id"] = (add_gl_line(bc, lines_url, sdesc, x["amount"], account)[0] if known
                            else _uncoded_line(bc, lines_url, sdesc, x["amount"]))
            db.upsert_entry(tok, r["id"], bc_splits=json.dumps(splits))
        if not x.get("done"):
            note = f"Split {i} of {n} of ${total:,.2f}."
            if i == 1 and first_note:
                note = f"{first_note} {note}"
            log.info("line '%s': G/L %s amount %.2f (split %d/%d)", sdesc, x["gl"], x["amount"], i, n)
            if known:
                finish_gl_line(bc, lines_url, x["line_id"], sdesc, x["amount"], account, bc.account_ok(account),
                               [(BC_DIM1_CODE, dim1), (BC_DIM2_CODE, dim2)], extra_note=note)
            else:
                for part in _chunks(f"{UNKNOWN_NOTE} {note}"):
                    bc.call("POST", lines_url, f"Add note for '{sdesc}'", json={"lineType": "Comment", "description": part})
            x["done"] = 1
            db.upsert_entry(tok, r["id"], bc_splits=json.dumps(splits))


def submit(assignment, rows, submitted_by: str, on_behalf: str | None = None) -> tuple[int, str]:
    """rows: db.transactions_for() rows, all complete. Returns (G/L lines on the invoice,
    name of the 2nd approver it went to - "" when there isn't one). on_behalf: the manager's name when
    their delegate submits for them ("signed by <delegate> on behalf of <manager>")."""
    who = f"{submitted_by} on behalf of {on_behalf}" if on_behalf else submitted_by
    signed_at = datetime.now(timezone.utc)                 # the moment Submit was pressed
    if not (BC_DIM1_CODE and BC_DIM2_CODE):
        raise BCError("BC_DIM1_CODE / BC_DIM2_CODE are not set in config/ocr.env")
    log.info("submit %s invoice %s (%d charges) bc.py %s", assignment["token"][:8], assignment["invoice_no"],
             len(rows), BC_PY_VERSION)
    assignment = db.get_assignment(assignment["token"]) or assignment   # fresh flags: never redo finished steps
    bc = BCClient(assignment["company_id"])
    inv_id, tok = assignment["invoice_id"], assignment["token"]
    inv_url = f"{bc.std}/purchaseInvoices({inv_id})"

    inv = bc.call("GET", inv_url, "Open purchase invoice")
    if inv.get("status") not in (None, "Draft", "Open"):
        raise BCError(f"Purchase invoice {assignment['invoice_no']} is {inv.get('status')}, not open.")
    lines_url = f"{inv_url}/purchaseInvoiceLines"

    # ---------------- cardholder block at the top of the invoice
    if not assignment.get("bc_header_done"):
        _post_header(bc, lines_url, assignment, rows, who)
        db.update_assignment(tok, bc_header_done=1)

    n_lines, all_gls, gl_amounts = 0, [], []
    for r in rows:
        when = (r["tran_date"] or "")[5:].replace("-", "/")          # 2026-09-14 -> 09/14
        desc = f"{when} {r['description']}".strip()[:100]
        gls = db.gl_list_of(r)
        all_gls += gls
        gl_amounts += list(zip(gls, split_for(r, gls) if len(gls) > 1 else [r["amount"]]))

        # ---------------- the G/L line(s) + dimensions + notes
        if len(gls) > 1 and not r["bc_line_id"]:
            _split_lines(bc, lines_url, tok, r, gls, desc)
            n_lines += len(gls)
        else:                                  # one G/L (or it went to BC as one line before it was split)
            _single_line(bc, lines_url, tok, r, gls[0], desc)
            n_lines += 1

        # ---------------- the receipt, attached to the invoice
        if r["file_path"] and not r["bc_attach_id"]:
            name = f"{when.replace('/', '-')} {r['file_name']}".strip()[:250]
            att_id = attach(bc, inv_url, name, Path(r["file_path"]).read_bytes(), f"receipt for '{desc}'")
            db.upsert_entry(tok, r["id"], bc_attach_id=att_id)

    # ---------------- is a 2nd approver waiting? (picked in BC when the statement was sent)
    rec = bc.call("GET", f"{bc.req}/RequisitionApprovalInvoices({inv_id})", "Read requisition status")
    two = rec.get("HigherRequisitionApproval") == "APPROVAL NOT SENT" and bool((rec.get("HigherRequisitionEmail") or "").strip())

    # ---------------- signed Requisition Form: Cost = total, G/L = the one string or MULTIPLE,
    # Description left blank, "<Approved|Signed> By: digitally signed by <user> at <Submit time>"
    sign_requisition_form(bc, inv_url, assignment["invoice_no"],
                          [r["amount"] for r in rows], all_gls, submitted_by, signed_at,
                          sign_label=SIGNED_BY if two else APPROVED_BY, on_behalf=on_behalf,
                          gl_amounts=gl_amounts)

    # ---------------- everything is on the invoice -> mark the requisition approved
    bc.call("PATCH", f"{bc.req}/RequisitionApprovalInvoices({inv_id})", "Set Mgr. Requisition Approval",
            json={"mgrRequisitionApproval": "APPROVED"}, headers={"If-Match": "*"})

    second = ""
    if two:
        second = rec.get("HigherRequisitionName") or rec.get("HigherRequisitionEmail")
        try:
            _send_second(bc, rec, assignment, rows, all_gls, who)
        except Exception as e:                    # the manager's part is done; the 2nd-approval sweep retries
            log.error("2nd approval email for %s failed: %s", assignment["invoice_no"], e)
            second += " (email will be retried)"
    _notify_sender(assignment, rows, gl_amounts, who, signed_at, second)
    return n_lines, second


def _notify_sender(assignment, rows, gl_amounts, who, signed_at, second) -> None:
    """Tell the BC user who sent the statement that the manager approved it (never fails the submit)."""
    from reqapproval import mailer

    to = assignment.get("sender_email")
    if not to:
        return
    gl, splits = gl_summary(gl_amounts)
    stmt = db.get_statement(assignment["statement_id"]) or {}
    closing = stmt.get("closing_date") or ""
    if len(closing) == 10:
        closing = f"{closing[5:7]}/{closing[8:]}/{closing[:4]}"
    total = round(sum(r["amount"] for r in rows), 2)
    text = (
        f"{who} approved the card statement for {assignment['cardholder']} (card ending {assignment['last4']}"
        + (f", closing {closing}" if closing else "") + f") at {_central(signed_at)}.\n\n"
        f"Purchase invoice - {assignment['invoice_no']}\n"
        f"Charges - {len(rows)}, total {_money(total)}\n"
        f"G/L - {gl or 'none'}\n"
        + "".join(f"    {s}\n" for s in splits)
        + (f"2nd approval - sent to {second}\n" if second else "")
        + "\nThe invoice lines, receipts and the signed Requisition Form are on the purchase invoice in "
          "Business Central.\n\n" + mailer.FOOTER)
    subject = f"Approved: {assignment['cardholder']} card statement - Invoice #{assignment['invoice_no']}"
    if mailer.notify(to, subject, text):
        log.info("approval notice for %s sent to %s", assignment["invoice_no"], to)


def _statement_attachment(bc, inv_url, rows) -> tuple[str, str] | None:
    """The statement PDF attached in BC when it was sent: the earliest attachment that is neither a
    Requisition Form nor a receipt the portal attached (their ids are recorded per charge)."""
    receipts = {r["bc_attach_id"] for r in rows if r["bc_attach_id"]}
    atts = [a for a in bc.call("GET", f"{inv_url}/documentAttachments", "Read invoice attachments").get("value", [])
            if a.get("id") not in receipts and "requisition" not in a.get("fileName", "").lower()]
    pdfs = [a for a in atts if a.get("fileName", "").lower().endswith(".pdf")] or atts
    pdfs.sort(key=lambda a: a.get("lastModifiedDateTime") or a.get("createdDateTime") or "")
    return (pdfs[0]["id"], pdfs[0].get("fileName", "")) if pdfs else None


def _send_second(bc, rec, assignment, rows, all_gls, submitted_by) -> None:
    from reqapproval.bc import send_second      # the Requisition Approval portal handles the 2nd approver
    gl, _ = gl_summary([(g, 0.0) for g in all_gls])
    inv_url = f"{bc.std}/purchaseInvoices({assignment['invoice_id']})"
    send_second(bc, rec, assignment["company_id"], source="card",
                attachment=_statement_attachment(bc, inv_url, db.transactions_for(assignment)),
                signer=submitted_by, amount=round(sum(r["amount"] for r in rows), 2), gl=gl)
