"""
Card-statement pages:
  * receipts_page (everyone): statements assigned to the signed-in user; receipt + G/L per charge
    (optionally split across several G/Ls) + an optional description; submit to BC
  * breakdown_page (admins): upload a statement and browse it by cardholder
"""
from __future__ import annotations

import hashlib
import logging
import json
import re
from datetime import date

import streamlit as st

from cards import store as db
from cards.bc_submit import GL_FORMAT, BCError, split_amount, submit
from cards.parsers import MERCHANTS, parse_statement, reconcile
from cards.receipts import check_lines, prepare_receipt, receipt_lines
from core.delegates import acting_for, principals_for
from core.identity import current_email, is_admin
from core.ocr_worker import OCRTimeout, run as run_ocr
from core.settings import READ_RECEIPTS, RECEIPT_DIR

log = logging.getLogger("ocr.portal")

MEMO_MAX = 250             # per-charge description (goes to BC as comment lines)

# A finished transaction's box turns translucent green. Streamlit gives a keyed container the CSS
# class "st-key-<key>", so complete boxes are keyed "txn_done_<id>" and styled here.
DONE_CSS = """
<style>
div[class*="st-key-txn_done_"] {
    background-color: rgba(34, 197, 94, 0.12);
    border-color: rgba(34, 197, 94, 0.55) !important;
    transition: background-color 0.3s ease;
}
</style>
"""


# ------------------------------------------------------------------ receipts page
CHECK_TEXT = {
    "match": ("success", "Receipt matches the charge amount and date."),
    "found": ("success", "Charge amount found on the receipt; date OK."),
}


def md(text: str) -> str:
    """Escape $ so Streamlit doesn't render '$642.89 ... $550.00' as a LaTeX formula."""
    return str(text).replace("$", "\\$")


def _d(value) -> str:
    """'2026-09-14' / date -> '09/14/2026'."""
    if not value:
        return ""
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return d.strftime("%m/%d/%Y")


def _clean_gl(raw: str) -> str:
    """Keep what the user typed (so bad characters can be flagged), but auto-insert the
    dashes when they type the 11 digits without them: 50100012000 -> 50100-01-2000."""
    raw = raw.strip()
    if re.fullmatch(r"\d{11}", raw):
        return f"{raw[:5]}-{raw[5:7]}-{raw[7:]}"
    return raw


def _save(token: str, txn_id: int, field: str, key: str):
    val = st.session_state[key].strip()
    db.upsert_entry(token, txn_id, **{field: _clean_gl(val) if field == "gl" else val})


# ---- G/L split: the list lives in gl_list (JSON) with the amounts in amt_list; gl holds the first one.
# A G/L the user doesn't know is stored as db.UNKNOWN.
def _entry(token: str, txn_id: int):
    return db.get_entry(token, txn_id)


def _entry_gls(token: str, txn_id: int) -> list[str]:
    e = _entry(token, txn_id)
    return db.gl_list_of(e) if e else [""]


def _entry_amts(token: str, txn_id: int) -> list:
    e = _entry(token, txn_id)
    return db.amounts_of(e) if e else [None]


def _resize(token: str, txn_id: int, n: int | None):
    gls, amts = _entry_gls(token, txn_id), _entry_amts(token, txn_id)
    if not n:                                   # back to one G/L
        db.upsert_entry(token, txn_id, split_n=None, gl=gls[0])
        return
    gls, amts = (gls + [""] * n)[:n], (amts + [None] * n)[:n]
    db.upsert_entry(token, txn_id, split_n=n, gl_list=json.dumps(gls), amt_list=json.dumps(amts), gl=gls[0])


def _toggle_split(token: str, txn_id: int, key: str):
    _resize(token, txn_id, max(2, len(_entry_gls(token, txn_id))) if st.session_state[key] else None)


def _set_split_n(token: str, txn_id: int, key: str):
    _resize(token, txn_id, int(st.session_state[key]))


def _save_split_gl(token: str, txn_id: int, i: int, key: str):
    gls = _entry_gls(token, txn_id)
    gls[i] = _clean_gl(st.session_state[key])
    db.upsert_entry(token, txn_id, gl_list=json.dumps(gls), gl=gls[0])


def _save_split_amt(token: str, txn_id: int, i: int, key: str):
    amts = _entry_amts(token, txn_id)
    amts[i] = _money_in(st.session_state[key])
    db.upsert_entry(token, txn_id, amt_list=json.dumps(amts))


def _toggle_unknown(token: str, txn_id: int, i: int | None, key: str):
    """i=None: the charge's only G/L; else split G/L number i."""
    val = db.UNKNOWN if st.session_state[key] else ""
    if i is None:
        db.upsert_entry(token, txn_id, gl=val)
        return
    gls = _entry_gls(token, txn_id)
    gls[i] = val
    db.upsert_entry(token, txn_id, gl_list=json.dumps(gls), gl=gls[0])


def _money_in(text) -> float | None:
    t = re.sub(r"[\s$,]", "", str(text or ""))
    try:
        v = round(abs(float(t)), 2)
    except ValueError:
        return None
    return v or None


def _split_problem(t) -> str | None:
    """Why the split amounts aren't usable yet ('' when they are, None for no split amounts)."""
    amts = db.amounts_of(t)
    if len(amts) < 2 or all(a is None for a in amts):
        return None                              # split evenly
    total = round(abs(t["amount"]), 2)
    entered = round(sum(a for a in amts if a), 2)
    if any(a is None for a in amts):
        return f"${total - entered:,.2f} left to assign - fill in every amount, or clear them all to split evenly."
    if entered != total:
        diff = round(total - entered, 2)
        return (f"Amounts add up to ${entered:,.2f}, not ${total:,.2f} "
                f"({f'${diff:,.2f} left to assign' if diff > 0 else f'${-diff:,.2f} too much'}).")
    return ""


def _toggle_missing(token: str, txn_id: int, key: str):
    if st.session_state[key]:
        # no receipt: drop any uploaded file and record the explanation as "Missing"
        db.upsert_entry(token, txn_id, check_status="missing", note="Missing",
                        file_path=None, file_name=None, file_sha=None, detected_total=None)
    else:
        db.upsert_entry(token, txn_id, check_status=None, note="")


def _gl_error(gl: str) -> str | None:
    if not gl or gl == db.UNKNOWN or GL_FORMAT.match(gl):
        return None
    if re.search(r"[^0-9-]", gl):
        return "Only numbers and dashes are allowed (xxxxx-yy-zzzz)."
    return "Use the format xxxxx-yy-zzzz (5 digits, 2 digits, 4 digits)."


def _complete(t) -> bool:
    gls = db.gl_list_of(t)
    if not all(g and (g == db.UNKNOWN or GL_FORMAT.match(g)) for g in gls):
        return False
    if _split_problem(t):
        return False
    if t["type"] == "Credit" or t["check_status"] == "missing":   # receipt not required
        return True
    if not t["file_path"]:
        return False
    return t["check_status"] in ("match", "found", "attached") or bool((t["note"] or "").strip())


def _label(a) -> str:
    s = (f"Invoice {a['invoice_no']} · {a['cardholder']} …{a['last4']} · closing {_d(a['closing_date'])}"
         f" · sent {_d(a['created_at'])}")
    if (who := acting_for(current_email(), a["manager_email"])):
        s += f" · for {who}"
    return s + (f" · {a['manager_email']}" if is_admin() else "")


def _coding(tok: str, t) -> None:
    """G/L (or several, with an amount each or split evenly; any of them can be "don't know")
    and the optional description for one charge."""
    tid = t["id"]
    gls = db.gl_list_of(t)
    skey = f"sp_{tok}_{tid}"
    split = st.checkbox("Split G/L", value=len(gls) > 1, key=skey, on_change=_toggle_split, args=(tok, tid, skey),
                        help="Divide this charge across several G/L accounts.")
    if split and len(gls) > 1:
        nkey = f"spn_{tok}_{tid}"
        st.number_input("How many G/Ls?", min_value=2, max_value=db.MAX_SPLITS, value=len(gls), step=1,
                        key=nkey, on_change=_set_split_n, args=(tok, tid, nkey))
        amts = db.amounts_of(t)
        even = split_amount(t["amount"], len(gls))
        for i, g in enumerate(gls):
            st.markdown(f"**G/L {i + 1}**")
            ukey = f"gu_{tok}_{tid}_{i}"
            unknown = st.checkbox("Don't know the G/L", value=g == db.UNKNOWN, key=ukey,
                                  on_change=_toggle_unknown, args=(tok, tid, i, ukey),
                                  help="This part goes to Business Central uncoded, for Accounts Payable to code.")
            if not unknown:
                key = f"g_{tok}_{tid}_{i}"
                st.text_input("G/L (xxxxx-yy-zzzz)", value="" if g == db.UNKNOWN else g, key=key,
                              placeholder="xxxxx-yy-zzzz", max_chars=13, on_change=_save_split_gl, args=(tok, tid, i, key))
                if err := _gl_error(g):
                    st.error(err)
            akey = f"ga_{tok}_{tid}_{i}"
            st.text_input("Amount", value="" if amts[i] is None else f"{amts[i]:.2f}", key=akey,
                          placeholder=f"{abs(even[i]):,.2f} (even split)",
                          on_change=_save_split_amt, args=(tok, tid, i, akey))
        if problem := _split_problem(t):
            st.warning(md(problem))
        elif all(a is None for a in amts):
            st.caption("Amounts blank: the charge is split evenly.")
    else:
        ukey = f"gu_{tok}_{tid}"
        unknown = st.checkbox("Don't know the G/L", value=t["gl"] == db.UNKNOWN, key=ukey,
                              on_change=_toggle_unknown, args=(tok, tid, None, ukey),
                              help="The charge goes to Business Central uncoded, for Accounts Payable to code.")
        if not unknown:
            key = f"g_{tok}_{tid}"
            st.text_input("G/L (xxxxx-yy-zzzz)", value="" if t["gl"] == db.UNKNOWN else (t["gl"] or ""), key=key,
                          placeholder="xxxxx-yy-zzzz", max_chars=13, on_change=_save, args=(tok, tid, "gl", key))
            if err := _gl_error(t["gl"]):
                st.error(err)

    mkey = f"d_{tok}_{tid}"
    st.text_area("Description", value=t["memo"] or "", key=mkey, max_chars=MEMO_MAX, height=68,
                 placeholder="Optional - what was this for?", on_change=_save, args=(tok, tid, "memo", mkey),
                 help="Goes to Business Central as a comment under this charge.")
    if _complete(t):
        st.caption("✅ Complete")


def receipts_page():
    st.title("🧾 Invoice Receipts")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)

    rows = db.assignments_for(current_email(), include_all=is_admin(), also=principals_for(current_email()))
    token = st.query_params.get("t")
    if not rows:
        a = db.get_assignment(token) if token else None
        if a and a["status"] == "submitted":
            st.info(f"That statement was already submitted on {a['submitted_at'][:10]}.")
        elif a:
            st.warning("That link was sent to a different account. Sign in with the account it was emailed to.")
        else:
            st.info("Nothing is waiting on you right now.")
        return

    idx = next((i for i, r in enumerate(rows) if r["token"] == token), 0)
    a = st.selectbox("Statement", rows, index=idx, format_func=_label)
    if st.query_params.get("t") != a["token"]:
        st.query_params["t"] = a["token"]
    tok = a["token"]
    on_behalf = acting_for(current_email(), a["manager_email"])
    if on_behalf:
        st.info(f"You're completing this statement on behalf of **{on_behalf}**. The Requisition Form will say "
                f"it was signed by you on behalf of {on_behalf}.")

    txns = db.transactions_for(a)
    done = sum(_complete(t) for t in txns)
    st.progress(done / len(txns) if txns else 1.0, text=f"{done} of {len(txns)} transactions complete")
    st.caption("For each charge: attach the receipt and enter the G/L string as **xxxxx-yy-zzzz** "
               "(account - department - program). Tick **Split G/L** to divide a charge across several G/Ls "
               "(enter each amount, or leave them blank to split evenly), or **Don't know the G/L** to leave it "
               "for Accounts Payable. The Description is optional.")

    st.markdown(DONE_CSS, unsafe_allow_html=True)
    for t in txns:
        state = "done" if _complete(t) else "todo"
        with st.container(border=True, key=f"txn_{state}_{t['id']}"):
            info, upload, coding = st.columns([3, 3, 2])

            with info:
                amt = abs(t["amount"])
                kind = "Credit" if t["type"] == "Credit" else ""
                st.markdown(md(f"**{t['description']}**  \n### ${amt:,.2f} {kind}"))
                st.caption(md(f"{_d(t['tran_date'])} · ref {t['reference']}" + (f" · MCC {t['mcc']}" if t["mcc"] else "")))
                if t["details"]:
                    st.caption(md(t["details"]))

            with upload:
                missing = False
                if t["type"] != "Credit":
                    mkey = f"m_{tok}_{t['id']}"
                    missing = st.checkbox("Receipt missing", value=t["check_status"] == "missing", key=mkey,
                                          on_change=_toggle_missing, args=(tok, t["id"], mkey),
                                          help="No receipt is available. The line goes to BC flagged as Missing.")
                if missing:
                    st.caption("⚠️ No receipt — will be flagged as **Missing** on the invoice line.")
                f = None if missing else st.file_uploader("Receipt", type=["pdf", "jpg", "jpeg", "png", "heic", "heif"],
                                     key=f"r_{tok}_{t['id']}",
                                     label_visibility="collapsed" if t["type"] != "Credit" else "visible",
                                     help="Photo or PDF of the itemized receipt")
                if f is not None:
                    data = f.getvalue()
                    sha = hashlib.sha256(data).hexdigest()
                    if sha != t["file_sha"] and not READ_RECEIPTS:
                        # Receipt reading is off: just attach the file (photos still become a small JPEG).
                        try:
                            stored, name = prepare_receipt(data, f.name)
                        except Exception as e:  # unsupported image: keep the original file
                            log.warning("receipt %s for charge %s not converted: %s", f.name, t["id"], e)
                            stored, name = data, f.name
                        folder = RECEIPT_DIR / tok
                        folder.mkdir(exist_ok=True)
                        safe = re.sub(r"[^\w.\-]", "_", name)
                        path = folder / f"{t['id']}_{safe}"
                        path.write_bytes(stored)
                        db.upsert_entry(tok, t["id"], file_path=str(path), file_name=name, file_sha=sha,
                                        check_status="attached", detected_total=None, detected_date=None)
                        st.rerun()
                    elif sha != t["file_sha"]:
                        with st.spinner("Reading receipt…"):
                            # Whatever happens while reading, the file is kept and the charge is updated
                            # below: a receipt that can't be read (or takes too long) is "unreadable",
                            # and the user can explain it or replace it.
                            stored, name, status, total, when = data, f.name, "unreadable", None, None
                            try:
                                stored, name = prepare_receipt(data, f.name)
                                lines = run_ocr(receipt_lines, stored, name)   # stopped after OCR_TIMEOUT_SECONDS
                                tdate = date.fromisoformat(t["tran_date"]) if t["tran_date"] else None
                                chk = check_lines(lines, t["amount"], tdate)
                                status, total, when = chk.status, chk.detected_total, chk.detected_date
                            except OCRTimeout as e:
                                log.warning("receipt %s for charge %s: %s", f.name, t["id"], e)
                            except Exception as e:  # corrupt file, unsupported image, etc.
                                log.warning("receipt %s for charge %s unreadable: %s", f.name, t["id"], e)
                            folder = RECEIPT_DIR / tok
                            folder.mkdir(exist_ok=True)
                            safe = re.sub(r"[^\w.\-]", "_", name)
                            path = folder / f"{t['id']}_{safe}"
                            path.write_bytes(stored)
                            db.upsert_entry(tok, t["id"], file_path=str(path), file_name=name, file_sha=sha,
                                            check_status=status, detected_total=total,
                                            detected_date=str(when or "") or None)
                        st.rerun()

                if missing:
                    pass
                elif t["file_name"]:
                    st.caption(md(f"📎 {t['file_name']}"))
                    read = " · ".join(x for x in [
                        md(f"${t['detected_total']:,.2f}") if t["detected_total"] is not None else "no total found",
                        _d(t["detected_date"]) if t["detected_date"] else "no date found"] if x)
                    if t["check_status"] == "attached":
                        pass                                   # not read (OCR_READ_RECEIPTS is off)
                    elif t["check_status"] in CHECK_TEXT:
                        st.success(CHECK_TEXT[t["check_status"]][1], icon="✅")
                        st.caption(f"Read from receipt: {read}")
                    elif t["check_status"] == "unreadable":
                        st.warning(md(f"Couldn't read **{t['file_name']}**. If it's the wrong file, upload the "
                                      "right one in its place. Otherwise explain below."), icon="⚠️")
                    else:
                        st.warning(md(f"Charge **${amt:,.2f}** on **{_d(t['tran_date'])}**"), icon="⚠️")
                        st.caption(f"Read from receipt: {read}. If it's the wrong file, upload the right one "
                                   "in its place. Otherwise explain below (e.g. tip added, partial receipt).")
                    if t["check_status"] not in CHECK_TEXT and t["check_status"] != "attached":
                        st.text_input("Explanation", value=t["note"] or "", key=f"n_{tok}_{t['id']}",
                                      on_change=_save, args=(tok, t["id"], "note", f"n_{tok}_{t['id']}"))
                elif t["type"] == "Credit":
                    st.caption("Receipt optional for credits.")

            with coding:
                _coding(tok, t)

    st.divider()
    if st.button(f"Submit {len(txns)} transactions to invoice {a['invoice_no']}", type="primary",
                 disabled=done < len(txns)):
        with st.spinner("Sending to Business Central…"):
            try:
                n, second = submit(a, db.transactions_for(a), current_email(), on_behalf=on_behalf)
            except BCError as e:
                st.error(md(f"Business Central stopped the submission. Anything already added to the invoice "
                                     f"won't be added twice when you resubmit.\n\n{e}"))
                return
        db.mark_submitted(tok, current_email())
        st.query_params.clear()
        st.session_state["flash"] = (f"Submitted {n} lines to purchase invoice {a['invoice_no']}. "
                                     + (f"Sent to {second} for the 2nd approval. " if second else "") + "Thank you!")
        st.rerun()


# ------------------------------------------------------------------ admin: statement breakdown
@st.cache_data(show_spinner="Parsing statement…")
def _parse(pdf: bytes, merchant: str, method: str):
    return parse_statement(pdf, merchant, method)


def breakdown_page():
    st.title("💳 Statement breakdown")
    c1, c2 = st.columns(2)
    merchant = c1.selectbox("Merchant", ["auto", *MERCHANTS],
                            format_func=lambda m: "Detect automatically" if m == "auto" else MERCHANTS[m].label)
    method = c2.selectbox("Extraction", ["auto", "text", "ocr"],
                          format_func={"auto": "Auto", "text": "PDF text layer only", "ocr": "Force docTR OCR"}.get)
    up = st.file_uploader("Statement PDF", type="pdf")
    if not up:
        return
    try:
        stmt = _parse(up.getvalue(), merchant, method)
    except ValueError as e:
        st.error(str(e))
        return
    recon = reconcile(stmt)
    st.caption(f"Closing {stmt.closing_date} · {len(stmt.transactions)} transactions · {stmt.method}")
    for w in stmt.warnings:
        st.warning(w)
    if (recon["matches"] == False).any():  # noqa: E712
        st.error("Some cardholder totals don't match the statement — see the Reconciled column.")

    ch = st.selectbox("Cardholder", recon.to_dict("records"), index=None, placeholder="All cardholders",
                      format_func=lambda r: f"{r['name']} · …{r['last4']}")
    money = st.column_config.NumberColumn(format="$%.2f")
    if ch is None:
        st.dataframe(recon, hide_index=True, width="stretch",
                     column_config={"limit": money, "statement_total": money, "parsed_purchases": money,
                                    "matches": st.column_config.CheckboxColumn("Reconciled")})
        return
    tx = stmt.for_holder(ch["last4"])
    p = tx.loc[tx["type"] == "Purchase", "amount"].sum()
    o = tx.loc[tx["type"] != "Purchase", "amount"].sum()
    m1, m2, m3 = st.columns(3)
    m1.metric("Purchases", f"${p:,.2f}")
    m2.metric("Payments / credits", f"${o:,.2f}")
    m3.metric("Net", f"${p + o:,.2f}")
    st.dataframe(tx.drop(columns=["name", "last4", "page"]), hide_index=True, width="stretch",
                 column_config={"amount": money})
    st.download_button("Download CSV", tx.to_csv(index=False).encode(),
                       f"{ch['name'].replace(',', '_')}_{ch['last4']}.csv", "text/csv")
