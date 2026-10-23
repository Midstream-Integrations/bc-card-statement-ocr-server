"""
2nd-approver sweep - run it from cron.

Normally the 2nd approver is emailed the moment the 1st approver presses Submit. This catches
anything that didn't go out (email error) and invoices whose 1st approval was done the old way:
every invoice with Mgr. Requisition Approval = APPROVED and 2nd Requisition Approval = APPROVAL NOT SENT
gets a portal link.

  cd /opt/ocr && .venv/bin/python -m reqapproval.secondary
Needs REQAPPROVAL_COMPANY_ID (BC company id) in config/ocr.env.
"""
from __future__ import annotations

import os
import sys

from core.bc_client import BCClient, log
from reqapproval import store as db
from reqapproval.bc import NOT_SENT, send_second


def is_card_invoice(invoice_id: str) -> bool:
    """A card statement was submitted to this invoice through the portal."""
    from core.db import conn
    with conn() as c:
        return c.execute("SELECT 1 FROM assignments WHERE invoice_id=? AND status='submitted' LIMIT 1",
                         (invoice_id,)).fetchone() is not None


def main() -> int:
    db.init()
    company_id = os.environ["REQAPPROVAL_COMPANY_ID"].strip("{}").lower()
    bc = BCClient(company_id)
    recs = bc.call("GET", f"{bc.req}/RequisitionApprovalInvoices", "Find pending 2nd approvals", params={
        "$filter": f"mgrRequisitionApproval eq 'APPROVED' and HigherRequisitionApproval eq '{NOT_SENT}'"}).get("value", [])
    failed = 0
    for rec in recs:
        inv_id = (rec.get("Guid") or "").strip("{}").lower()
        if not inv_id or not (rec.get("HigherRequisitionEmail") or "").strip():
            log.warning("2nd approval for %s has no Guid/email; skipped", rec.get("InvoiceNo"))
            continue
        try:
            first = db.last_submitted(inv_id, 1)
            if first:
                send_second(bc, rec, company_id, first=first)
            elif is_card_invoice(inv_id):
                send_second(bc, rec, company_id, source="card")   # shows the earliest attachment = the statement
            else:
                send_second(bc, rec, company_id)
        except Exception as e:
            failed += 1
            log.error("2nd approval for %s failed: %s", rec.get("InvoiceNo"), e)
    log.info("2nd-approval sweep: %d pending, %d failed", len(recs), failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
