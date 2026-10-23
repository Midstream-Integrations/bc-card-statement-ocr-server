"""Card-statement tables (statements, transactions, assignments, receipt entries, cardholders)."""
from __future__ import annotations

import json

from core.db import add_column, conn, now

MAX_SPLITS = 10            # G/L accounts one charge can be split across
UNKNOWN = "UNKNOWN"        # stored as the G/L when the user ticks "Don't know the G/L"

SCHEMA = """
CREATE TABLE IF NOT EXISTS statements (
  id TEXT PRIMARY KEY, merchant TEXT, closing_date TEXT, filename TEXT,
  pdf_path TEXT, method TEXT, uploaded_at TEXT);
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, statement_id TEXT, last4 TEXT, name TEXT, type TEXT,
  post_date TEXT, tran_date TEXT, description TEXT, reference TEXT, amount REAL,
  mcc TEXT, details TEXT, seq INTEGER);
CREATE TABLE IF NOT EXISTS assignments (
  token TEXT PRIMARY KEY, statement_id TEXT, last4 TEXT, cardholder TEXT,
  invoice_id TEXT, invoice_no TEXT, company_id TEXT,
  manager_email TEXT, manager_name TEXT,
  status TEXT DEFAULT 'open', created_at TEXT, submitted_at TEXT, submitted_by TEXT);
CREATE TABLE IF NOT EXISTS receipt_entries (
  txn_id INTEGER, token TEXT, gl TEXT DEFAULT '', note TEXT DEFAULT '',
  file_path TEXT, file_name TEXT, file_sha TEXT,
  check_status TEXT, detected_total REAL, updated_at TEXT,
  PRIMARY KEY (txn_id, token));
CREATE TABLE IF NOT EXISTS cardholders (
  statement_id TEXT, last4 TEXT, name TEXT, role TEXT, credit_limit REAL, statement_total REAL,
  PRIMARY KEY (statement_id, last4));
CREATE INDEX IF NOT EXISTS ix_tx_stmt ON transactions(statement_id, last4);
CREATE INDEX IF NOT EXISTS ix_asg_mgr ON assignments(manager_email, status);
"""


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        add_column(c, "statements", "sha", "TEXT")                 # same PDF uploaded twice -> same statement
        add_column(c, "receipt_entries", "detected_date", "TEXT")  # date read from the receipt
        add_column(c, "receipt_entries", "bc_line_id", "TEXT")     # set once the line exists on the BC invoice
        add_column(c, "receipt_entries", "bc_attach_id", "TEXT")   # set once the receipt is attached in BC
        add_column(c, "receipt_entries", "bc_line_done", "INTEGER")  # dimensions + notes also done
        add_column(c, "receipt_entries", "split_n", "INTEGER")     # >1: the charge is split across this many G/Ls
        add_column(c, "receipt_entries", "gl_list", "TEXT")        # JSON list of those G/L strings
        add_column(c, "receipt_entries", "memo", "TEXT")           # the user's description of the charge
        add_column(c, "receipt_entries", "amt_list", "TEXT")       # JSON amount per split G/L (null = split evenly)
        add_column(c, "receipt_entries", "bc_splits", "TEXT")      # JSON [{gl, amount, line_id, done}] in BC
        add_column(c, "assignments", "bc_header_done", "INTEGER")     # cardholder comment block on the invoice
        add_column(c, "assignments", "bc_stmt_attach_id", "TEXT")     # statement PDF attached to the invoice
        add_column(c, "assignments", "sender_email", "TEXT")          # BC user who sent it; told when it's approved
        # one open link per invoice + card: keep the one with the most work done (then the newest)
        c.execute("""UPDATE assignments SET status='superseded'
                     WHERE status='open' AND token NOT IN (
                       SELECT token FROM (
                         SELECT a.token, ROW_NUMBER() OVER (
                           PARTITION BY a.invoice_id, a.last4
                           ORDER BY (SELECT COUNT(*) FROM receipt_entries e WHERE e.token = a.token) DESC,
                                    a.created_at DESC) AS rn
                         FROM assignments a WHERE a.status='open') WHERE rn = 1)""")


def gl_list_of(row) -> list[str]:
    """The G/L string(s) for a charge: one, or split_n of them when it's split."""
    n = row["split_n"] or 0
    if n > 1:
        try:
            gls = json.loads(row["gl_list"] or "[]")
        except ValueError:
            gls = []
        gls = [str(g or "") for g in gls][:n]
        return gls + [""] * (n - len(gls))
    return [row["gl"] or ""]


def amounts_of(row) -> list:
    """The user's amount for each split G/L (None where blank), one per G/L in gl_list_of()."""
    n = len(gl_list_of(row))
    try:
        amts = json.loads(row["amt_list"] or "[]")
    except (KeyError, IndexError, ValueError, TypeError):
        amts = []
    amts = list(amts)[:n]
    return amts + [None] * (n - len(amts))


def find_statement_by_sha(sha, merchant):
    with conn() as c:
        return c.execute("SELECT * FROM statements WHERE sha=? AND merchant=?", (sha, merchant)).fetchone()


def save_statement(sid, st, filename, pdf_path, sha=None):
    with conn() as c:
        c.execute("""INSERT INTO statements (id, merchant, closing_date, filename, pdf_path, method, uploaded_at, sha)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (sid, st.merchant, str(st.closing_date or ""), filename, str(pdf_path), st.method, now(), sha))
        _save_cardholders(c, sid, st)
        for seq, r in enumerate(st.transactions.itertuples(index=False)):
            c.execute("""INSERT INTO transactions (statement_id,last4,name,type,post_date,tran_date,
                         description,reference,amount,mcc,details,seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (sid, r.last4, r.name, r.type, str(r.post_date or ""), str(r.tran_date or ""),
                       r.description, r.reference, float(r.amount), r.mcc, r.details, seq))


def _save_cardholders(c, sid, st):
    for h in st.cardholders.itertuples(index=False):
        c.execute("INSERT OR REPLACE INTO cardholders VALUES (?,?,?,?,?,?)",
                  (sid, h.last4, h.name, h.role,
                   None if h.limit is None else float(h.limit),
                   None if h.statement_total is None else float(h.statement_total)))


def cardholder_info(statement_id, last4):
    """Cardholder summary row from the statement; statements uploaded before this table existed
    are re-parsed from the saved PDF once."""
    with conn() as c:
        row = c.execute("SELECT * FROM cardholders WHERE statement_id=? AND last4=?", (statement_id, last4)).fetchone()
        if row:
            return row
        stmt = c.execute("SELECT * FROM statements WHERE id=?", (statement_id,)).fetchone()
    if not stmt:
        return None
    from pathlib import Path
    from cards.parsers import parse_statement
    try:
        st = parse_statement(Path(stmt["pdf_path"]).read_bytes(), stmt["merchant"])
    except Exception:
        return None
    with conn() as c:
        _save_cardholders(c, statement_id, st)
        return c.execute("SELECT * FROM cardholders WHERE statement_id=? AND last4=?", (statement_id, last4)).fetchone()


def update_assignment(token, **fields):
    with conn() as c:
        c.execute(f"UPDATE assignments SET {', '.join(f'{k}=?' for k in fields)} WHERE token=?",
                  (*fields.values(), token))


def get_statement(sid):
    with conn() as c:
        return c.execute("SELECT * FROM statements WHERE id=?", (sid,)).fetchone()


def find_open_assignment(statement_id, last4, invoice_id):
    with conn() as c:
        return c.execute("""SELECT * FROM assignments WHERE statement_id=? AND last4=? AND invoice_id=?
                            AND status='open'""", (statement_id, last4, invoice_id)).fetchone()


def create_assignment(**a):
    with conn() as c:
        # a newer link for the same invoice + card replaces any older open one
        c.execute("UPDATE assignments SET status='superseded' WHERE invoice_id=? AND last4=? AND status='open'",
                  (a["invoice_id"], a["last4"]))
        c.execute("""INSERT INTO assignments (token,statement_id,last4,cardholder,invoice_id,invoice_no,
                     company_id,manager_email,manager_name,created_at,sender_email) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (a["token"], a["statement_id"], a["last4"], a["cardholder"], a["invoice_id"], a["invoice_no"],
                   a["company_id"], a["manager_email"].lower(), a["manager_name"], now(),
                   (a.get("sender_email") or "").strip().lower() or None))


def get_assignment(token):
    with conn() as c:
        return c.execute("SELECT * FROM assignments WHERE token=?", (token,)).fetchone()


def assignments_for(email, include_all=False, also=()):
    """Open statements for this manager, plus anyone in `also` (people they're a delegate for)."""
    with conn() as c:
        q = """SELECT a.*, s.closing_date, s.merchant FROM assignments a JOIN statements s ON s.id=a.statement_id
               WHERE a.status='open' {} ORDER BY a.created_at DESC"""
        if include_all:
            return c.execute(q.format("")).fetchall()
        emails = [email.lower(), *(e.lower() for e in also)]
        return c.execute(q.format(f"AND a.manager_email IN ({', '.join('?' * len(emails))})"), emails).fetchall()


def transactions_for(assignment):
    """Charges/credits the user must document (payments to the bank are excluded)."""
    with conn() as c:
        return c.execute("""SELECT t.*, e.gl, e.note, e.file_name, e.file_path, e.file_sha,
                                   e.check_status, e.detected_total, e.detected_date, e.bc_line_id, e.bc_attach_id,
                                   e.bc_line_done, e.split_n, e.gl_list, e.memo, e.bc_splits, e.amt_list
                            FROM transactions t
                            LEFT JOIN receipt_entries e ON e.txn_id=t.id AND e.token=?
                            WHERE t.statement_id=? AND t.last4=? AND t.type!='Payment'
                            ORDER BY t.seq""",
                         (assignment["token"], assignment["statement_id"], assignment["last4"])).fetchall()


def get_entry(token, txn_id):
    with conn() as c:
        return c.execute("SELECT * FROM receipt_entries WHERE txn_id=? AND token=?", (txn_id, token)).fetchone()


def upsert_entry(token, txn_id, **fields):
    fields["updated_at"] = now()
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO receipt_entries (txn_id, token) VALUES (?,?)", (txn_id, token))
        c.execute(f"UPDATE receipt_entries SET {', '.join(f'{k}=?' for k in fields)} WHERE txn_id=? AND token=?",
                  (*fields.values(), txn_id, token))


def mark_submitted(token, email):
    with conn() as c:
        c.execute("UPDATE assignments SET status='submitted', submitted_at=?, submitted_by=? WHERE token=?",
                  (now(), email, token))
