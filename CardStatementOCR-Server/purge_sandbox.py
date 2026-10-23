"""
Remove portal data that belongs to sandbox invoices (everything created while the OCR server pointed
at the BC sandbox). An item is removed only when its invoice doesn't exist in the environment the
server points at now (BC_ENVIRONMENT in config/ocr.env) - real Production work is never touched.

  cd /opt/ocr && set -a && . config/ocr.env && set +a
  .venv/bin/python purge_sandbox.py            # dry run: shows what would be removed
  .venv/bin/python purge_sandbox.py --apply    # backs up the database, then removes it

Removed:
  * Requisition Approval links (both stages) for missing invoices
  * card statement links for missing invoices, with their receipt entries and uploaded receipt files
  * statements (and their transactions / cardholders / stored PDF) no remaining link uses
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from core.bc_client import BCClient, BCError
from core.db import conn
from core.settings import DB_PATH, RECEIPT_DIR

APPLY = "--apply" in sys.argv
ENV = os.environ.get("BC_ENVIRONMENT", "?")


def tables() -> set[str]:
    with conn() as c:
        return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


_clients: dict[str, BCClient] = {}
_exists: dict[tuple[str, str], bool] = {}


def invoice_exists(company_id: str, invoice_id: str) -> bool:
    key = (company_id, invoice_id)
    if key not in _exists:
        bc = _clients.setdefault(company_id, BCClient(company_id))
        try:
            bc.call("GET", f"{bc.std}/purchaseInvoices({invoice_id})", "check invoice")
            _exists[key] = True
        except BCError as e:
            if "404" not in str(e) and "NotFound" not in str(e):
                raise SystemExit(f"Stopped - couldn't check invoice {invoice_id}: {e}")   # never guess
            _exists[key] = False
    return _exists[key]


def main() -> None:
    have = tables()
    print(f"BC environment: {ENV}   ({'APPLY' if APPLY else 'dry run - nothing will be changed'})\n")

    # ---------------- Requisition Approval links
    req_gone = []
    if "reqapproval" in have:
        with conn() as c:
            rows = c.execute("SELECT token, stage, invoice_no, invoice_id, company_id, status, created_at "
                             "FROM reqapproval").fetchall()
        for r in rows:
            if not invoice_exists(r["company_id"], r["invoice_id"]):
                req_gone.append(r)
        print(f"Requisition Approval links: {len(rows)} total, {len(req_gone)} for invoices not in {ENV}")
        for r in req_gone:
            print(f"   - invoice {r['invoice_no']:>8}  stage {r['stage']}  {r['status']:<10}  {str(r['created_at'])[:16]}")

    # ---------------- card statement links
    card_gone = []
    if "assignments" in have:
        with conn() as c:
            rows = c.execute("SELECT token, statement_id, invoice_no, invoice_id, company_id, cardholder, status, created_at "
                             "FROM assignments").fetchall()
        for r in rows:
            if not invoice_exists(r["company_id"], r["invoice_id"]):
                card_gone.append(r)
        print(f"\nCard statement links: {len(rows)} total, {len(card_gone)} for invoices not in {ENV}")
        for r in card_gone:
            print(f"   - invoice {r['invoice_no']:>8}  {r['cardholder'] or '':<20}  {r['status']:<10}  {str(r['created_at'])[:16]}")

    # ---------------- statements nothing will use any more
    stmt_gone = []
    if "statements" in have:
        gone_tokens = {r["token"] for r in card_gone}
        with conn() as c:
            used = {r["statement_id"] for r in c.execute("SELECT token, statement_id FROM assignments").fetchall()
                    if r["token"] not in gone_tokens} if "assignments" in have else set()
            stmt_gone = [r for r in c.execute("SELECT id, merchant, closing_date, pdf_path FROM statements").fetchall()
                         if r["id"] not in used]
        print(f"\nCard statements no remaining link uses: {len(stmt_gone)}")
        for r in stmt_gone:
            print(f"   - {r['merchant']} closing {r['closing_date']}")

    if not (req_gone or card_gone or stmt_gone):
        print("\nNothing to remove.")
        return
    if not APPLY:
        print("\nDry run only. Run again with --apply to remove the items above.")
        return

    # ---------------- backup, then remove
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(DB_PATH).with_name(f"{Path(DB_PATH).stem}.before-purge-{stamp}{Path(DB_PATH).suffix}")
    shutil.copy2(DB_PATH, backup)
    print(f"\nBackup: {backup}")

    files = 0
    with conn() as c:
        for r in req_gone:
            c.execute("DELETE FROM reqapproval WHERE token=?", (r["token"],))
        for r in card_gone:
            if "receipt_entries" in have:
                c.execute("DELETE FROM receipt_entries WHERE token=?", (r["token"],))
            c.execute("DELETE FROM assignments WHERE token=?", (r["token"],))
            folder = Path(RECEIPT_DIR) / r["token"]
            if folder.is_dir():
                files += sum(1 for _ in folder.iterdir())
                shutil.rmtree(folder)
        for r in stmt_gone:
            for t in ("transactions", "cardholders"):
                if t in have:
                    c.execute(f"DELETE FROM {t} WHERE statement_id=?", (r["id"],))
            c.execute("DELETE FROM statements WHERE id=?", (r["id"],))
            if r["pdf_path"] and Path(r["pdf_path"]).is_file():
                Path(r["pdf_path"]).unlink()
                files += 1
    print(f"Removed {len(req_gone)} Requisition Approval links, {len(card_gone)} card links, "
          f"{len(stmt_gone)} statements and {files} stored files.")


if __name__ == "__main__":
    main()
