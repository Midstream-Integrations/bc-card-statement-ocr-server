"""Requisition-approval links (one row per approver: stage 1 = Requisition Manager, stage 2 = 2nd approver)."""
from __future__ import annotations

from core.db import add_column, conn, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS reqapproval (
  token TEXT PRIMARY KEY, stage INTEGER, invoice_id TEXT, invoice_no TEXT, company_id TEXT,
  vendor_no TEXT, vendor_name TEXT, vendor_invoice_no TEXT,
  attachment_id TEXT, attachment_name TEXT,
  approver_email TEXT, approver_name TEXT, creator_email TEXT,
  amount REAL, gl TEXT,
  status TEXT DEFAULT 'open', created_at TEXT, emailed_at TEXT,
  submitted_at TEXT, submitted_by TEXT,
  form_done INTEGER, approval_done INTEGER, second_done INTEGER);
CREATE INDEX IF NOT EXISTS ix_ra_inv ON reqapproval(invoice_id, stage, status);
CREATE INDEX IF NOT EXISTS ix_ra_who ON reqapproval(approver_email, status);
"""

FIELDS = ("token", "stage", "invoice_id", "invoice_no", "company_id", "vendor_no", "vendor_name",
          "vendor_invoice_no", "attachment_id", "attachment_name", "approver_email", "approver_name",
          "creator_email", "amount", "gl", "splits", "description", "source")


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        add_column(c, "reqapproval", "first_signer", "TEXT")   # stage 2: who signed "Signed By:"
        add_column(c, "reqapproval", "bc_line_id", "TEXT")      # G/L line on the invoice (stage 1)
        add_column(c, "reqapproval", "bc_line_done", "INTEGER") # dimensions + notes on that line done (single G/L)
        add_column(c, "reqapproval", "source", "TEXT")          # requisition (vendor invoice) or card (statement)
        add_column(c, "reqapproval", "description", "TEXT")     # approver's explanation (Requisition Form Description)
        add_column(c, "reqapproval", "splits", "TEXT")          # JSON [{gl, amount, line_id, done}] - one per G/L line


def get(token):
    with conn() as c:
        return c.execute("SELECT * FROM reqapproval WHERE token=?", (token,)).fetchone()


def find_open(invoice_id, stage):
    with conn() as c:
        return c.execute("SELECT * FROM reqapproval WHERE invoice_id=? AND stage=? AND status='open' "
                         "ORDER BY created_at DESC LIMIT 1", (invoice_id, stage)).fetchone()


def last_submitted(invoice_id, stage):
    with conn() as c:
        return c.execute("SELECT * FROM reqapproval WHERE invoice_id=? AND stage=? AND status='submitted' "
                         "ORDER BY submitted_at DESC LIMIT 1", (invoice_id, stage)).fetchone()


def create(replace: bool = True, **a):
    """A new link for the same invoice + stage replaces any older open one (BC re-send).
    replace=False: leave the older links alone until activate() - used when the new link still has
    to be emailed, so a failed email never retires the link the approver already has."""
    a["approver_email"] = (a.get("approver_email") or "").lower()
    row = {k: a.get(k) for k in FIELDS}
    with conn() as c:
        if replace:
            c.execute("UPDATE reqapproval SET status='superseded' WHERE invoice_id=? AND stage=? AND status='open'",
                      (row["invoice_id"], row["stage"]))
        c.execute(f"INSERT INTO reqapproval ({', '.join(row)}, first_signer, created_at) "
                  f"VALUES ({', '.join('?' * len(row))}, ?, ?)",
                  (*row.values(), a.get("first_signer"), now()))


def activate(token):
    """The new link's email went out: retire every other open link for the same invoice + stage."""
    with conn() as c:
        r = c.execute("SELECT invoice_id, stage FROM reqapproval WHERE token=?", (token,)).fetchone()
        if r:
            c.execute("UPDATE reqapproval SET status='superseded' WHERE invoice_id=? AND stage=? "
                      "AND status='open' AND token<>?", (r["invoice_id"], r["stage"], token))


def discard(token):
    """The new link's email failed (BC undoes the send): drop the link nobody received."""
    with conn() as c:
        c.execute("DELETE FROM reqapproval WHERE token=? AND status='open'", (token,))


def newer_link(req):
    """A later link for the same invoice + stage that is (or was) live, or None."""
    with conn() as c:
        return c.execute("SELECT * FROM reqapproval WHERE invoice_id=? AND stage=? AND created_at>? "
                         "AND status IN ('open','submitted') ORDER BY created_at DESC LIMIT 1",
                         (req["invoice_id"], req["stage"], req["created_at"])).fetchone()


def update(token, **fields):
    with conn() as c:
        c.execute(f"UPDATE reqapproval SET {', '.join(f'{k}=?' for k in fields)} WHERE token=?",
                  (*fields.values(), token))


def mark_submitted(token, email, signed_at_iso):
    update(token, status="submitted", submitted_at=signed_at_iso, submitted_by=email)


def open_for(email, include_all=False, also=()):
    """Open links for this person, plus anyone in `also` (people they're a delegate for)."""
    with conn() as c:
        if include_all:
            return c.execute("SELECT * FROM reqapproval WHERE status='open' ORDER BY created_at DESC").fetchall()
        emails = [email.lower(), *(e.lower() for e in also)]
        return c.execute(f"SELECT * FROM reqapproval WHERE status='open' AND approver_email IN "
                         f"({', '.join('?' * len(emails))}) ORDER BY created_at DESC", emails).fetchall()
