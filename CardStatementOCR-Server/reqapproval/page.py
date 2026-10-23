"""
Requisition Approval page (Streamlit). Link: {PUBLIC_URL}/?r=<token>

The approver sees the vendor invoice PDF from BC, enters the amount to pay and the G/L
(xxxxx-yy-zzzz) - or ticks "Split across multiple G/L accounts" and enters several, in which case
the amount is divided evenly between them - and presses Submit. The 2nd approver sees the invoice and the 1st approver's
signed Requisition Form, and presses Approve.

Called from the portal's app after M365 sign-in:
    from reqapproval import page as reqapproval_page
    reqapproval_page.render(token, user_email, is_admin)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pymupdf as fitz
import streamlit as st

from core.bc_client import BCClient, BCError
from core.delegates import acting_for, may_open, principals_for
from core.reqform import _central, _money
from reqapproval import store as db
from reqapproval.bc import (GL_FORMAT, MAX_DESCRIPTION, MAX_SPLITS, UNKNOWN, check_split, attachments, download, find_form, load_splits, portal_url,
                             split_amount, submit)


# ------------------------------------------------------------------ PDFs from BC (cached per session)
@st.cache_data(ttl=3600, show_spinner="Loading the invoice from Business Central...")
def _attachment(company_id: str, invoice_id: str, attachment_id: str) -> bytes:
    return download(BCClient(company_id), invoice_id, attachment_id)


@st.cache_data(ttl=600, show_spinner="Loading the Requisition Form...")
def _signed_form(company_id: str, invoice_id: str, invoice_no: str) -> bytes | None:
    bc = BCClient(company_id)
    form = find_form(attachments(bc, invoice_id), invoice_no, "signed")
    return download(bc, invoice_id, form["id"]) if form else None


@st.cache_data(show_spinner=False)
def _pages(data: bytes) -> list[bytes]:
    """PNG of each page. Opens PDFs and images (a scanned JPG/PNG attached in BC); [] if unreadable."""
    try:
        with fitz.open(stream=data) as doc:
            return [p.get_pixmap(dpi=130).tobytes("png") for p in doc]
    except Exception:
        return []


def _show_pdf(data: bytes, name: str, key: str):
    is_pdf = data[:4] == b"%PDF"
    st.download_button("Download", data, file_name=name or ("invoice.pdf" if is_pdf else "invoice"),
                       mime="application/pdf" if is_pdf else "application/octet-stream", key=key)
    pages = _pages(data)
    if not pages:
        st.warning("This attachment can't be shown here - use Download to open it.")
    for png in pages:
        st.image(png, width="stretch")


def _parse_amount(text: str) -> float | None:
    t = re.sub(r"[\s$,]", "", text or "")
    neg = t.startswith("(") and t.endswith(")")
    try:
        v = round(float(t.strip("()")), 2)
    except ValueError:
        return None
    return -v if neg else v


def _md(text: str) -> str:
    """Escape $ so Streamlit markdown shows amounts as text, not math."""
    return (text or "").replace("$", "\\$")


def _show_split(req) -> None:
    splits = load_splits(req)
    if len(splits) > 1:
        st.markdown("  \n".join(_md(f"`{x['gl']}` {_money(x['amount'])}") if x["gl"] != UNKNOWN
                                 else _md(f"G/L not known {_money(x['amount'])}") for x in splits))
    if req["description"]:
        st.markdown(f"**Description:** {_md(req['description'])}")


def _when(iso: str | None) -> str:
    try:
        return _central(datetime.fromisoformat(iso))
    except (TypeError, ValueError):
        return iso or ""


# ------------------------------------------------------------------ page
def _choice_label(r, user_email: str, is_admin: bool) -> str:
    stage = "2nd approval" if r["stage"] == 2 else "1st approval"
    s = (f"Invoice {r['invoice_no']} · {r['vendor_name'] or 'vendor'} · {stage} · sent {_when(r['created_at'])[:10]}")
    if (who := acting_for(user_email, r["approver_email"])):
        s += f" · for {who}"
    return s + (f" · {r['approver_email']}" if is_admin else "")


def approvals_page() -> None:
    """Every open Requisition Approval for the signed-in user - 1st and 2nd approvals, and any they're a
    delegate for - in one dropdown. An emailed link (?r=<token>) pre-selects its invoice."""
    user_email = st.session_state.get("_ocr_email", "")
    is_admin = bool(st.session_state.get("_ocr_admin"))
    st.title("✅ Requisition Approvals")
    rows = db.open_for(user_email, include_all=is_admin, also=principals_for(user_email))
    token = st.query_params.get("r")
    open_tokens = [r["token"] for r in rows]

    if token and token not in open_tokens:          # submitted, replaced, or someone else's: show what it is
        render(token, user_email, is_admin, embedded=True)
        if rows:
            st.divider()
            st.subheader("Other approvals waiting for you")
            if st.button("Show them"):
                st.query_params["r"] = rows[0]["token"]
                st.rerun()
        return
    if not rows:
        st.info("No requisition approvals are waiting on you right now.")
        return

    idx = open_tokens.index(token) if token in open_tokens else 0
    choice = st.selectbox("Invoice", rows, index=idx, format_func=lambda r: _choice_label(r, user_email, is_admin))
    if st.query_params.get("r") != choice["token"]:
        st.query_params["r"] = choice["token"]
    render(choice["token"], user_email, is_admin, embedded=True)


def render(token: str, user_email: str, is_admin: bool = False, embedded: bool = False) -> None:
    req = db.get(token)
    if not req:
        st.error("This approval link isn't valid. Use the link in the most recent email, or contact IT@example.com.")
        return
    if not is_admin and not may_open(user_email, req["approver_email"]):
        st.error(f"This approval was sent to {req['approver_email']}. You're signed in as {user_email}.")
        return
    on_behalf = acting_for(user_email, req["approver_email"])
    if req["status"] == "superseded":
        newer = db.newer_link(req)
        if newer and newer["status"] == "submitted":
            st.info(f"Invoice {req['invoice_no']} was already approved by {newer['submitted_by']} "
                    f"using a newer link.")
        elif newer:
            st.warning(f"A newer approval request was sent for invoice {req['invoice_no']}"
                       + (f" to {newer['approver_email']}" if newer["approver_email"] else "")
                       + ". Use the link in the latest email.")
        else:
            st.warning(f"This approval link for invoice {req['invoice_no']} is no longer active. "
                       "Ask Accounts Payable to send the invoice for Requisition Approval again.")
        return

    stage2 = req["stage"] == 2
    heading = f"{'2nd ' if stage2 else ''}Requisition Approval - Invoice {req['invoice_no']}"
    (st.subheader if embedded else st.title)(heading)
    st.caption(f"{req['vendor_name'] or ''} ({req['vendor_no'] or ''})  ·  Vendor invoice {req['vendor_invoice_no'] or ''}")
    if on_behalf and req["status"] == "open":
        st.info(f"You're approving on behalf of **{on_behalf}**. The Requisition Form will say it was signed "
                f"by you on behalf of {on_behalf}.")

    note = st.session_state.pop(f"ra_msg_{token}", None)
    if note:
        st.success(note)
    elif req["status"] == "submitted":
        st.success(f"Submitted by {req['submitted_by']} at {_when(req['submitted_at'])}.")

    left, right = st.columns([3, 2], gap="large")

    with left:
        doc_label = "Card statement" if (req["source"] or "") == "card" else "Vendor invoice"
        tabs = st.tabs([doc_label, "Requisition Form"]) if stage2 else [st.container()]
        with tabs[0]:
            if req["attachment_id"]:
                try:
                    _show_pdf(_attachment(req["company_id"], req["invoice_id"], req["attachment_id"]),
                              req["attachment_name"], "dl_inv")
                except BCError as e:
                    st.error(f"Couldn't load the invoice from Business Central: {e}")
            else:
                st.info(f"No {doc_label.lower()} is attached in Business Central.")
        if stage2:
            with tabs[1]:
                try:
                    form = _signed_form(req["company_id"], req["invoice_id"], req["invoice_no"])
                    if form:
                        _show_pdf(form, f"RequisitionForm-{req['invoice_no']}-signed.pdf", "dl_form")
                    else:
                        st.warning("The signed Requisition Form isn't on the invoice yet.")
                except BCError as e:
                    st.error(f"Couldn't load the Requisition Form: {e}")

    with right:
        if req["status"] == "submitted":
            return
        if stage2:
            if req["first_signer"]:
                st.markdown(f"**Signed by:** {req['first_signer']}")
            if req["amount"] is not None:
                st.markdown(_md(f"**Amount:** {_money(req['amount'])}  \n**G/L:** {req['gl']}"))
                _show_split(req)
            st.write("Review the invoice and the signed Requisition Form, then approve.")
            with st.form("approve2"):
                go = st.form_submit_button("Approve", type="primary", width="stretch")
            amount, gls, amounts, description = None, [], None, ""
        else:
            st.write("Enter the amount to pay and the G/L account, then press Submit. "
                     "Your digital signature and the time you press Submit go on the Requisition Form.")
            multi = st.checkbox("Split across multiple G/L accounts", key=f"ra_multi_{token}")
            n = int(st.number_input("How many G/L accounts?", min_value=2, max_value=MAX_SPLITS, value=2,
                                    step=1, key=f"ra_n_{token}")) if multi else 1
            with st.form("approve1"):
                amount_txt = st.text_input("Invoice amount to pay (total)" if multi else "Invoice amount to pay",
                                           placeholder="1,234.56")
                gl_txt, amt_txt, unknown = [], [], []
                for i in range(n):
                    cols = st.columns([5, 3, 3]) if multi else st.columns([8, 3])
                    gl_txt.append(cols[0].text_input(
                        f"G/L account {i + 1} (xxxxx-yy-zzzz)" if multi else "G/L account (xxxxx-yy-zzzz)",
                        placeholder="50200-01-1000", max_chars=13, key=f"ra_gl_{token}_{i}",
                        help="Format xxxxx-yy-zzzz (account-division-department)").strip())
                    if multi:
                        amt_txt.append(cols[1].text_input(f"Amount {i + 1}", placeholder="even split",
                                                          key=f"ra_amt_{token}_{i}"))
                    unknown.append(cols[-1].checkbox("Don't know the G/L", key=f"ra_unk_{token}_{i}",
                                                     help="Leave this G/L blank - it goes to Business Central "
                                                          "uncoded, for Accounts Payable to code."))
                if multi:
                    st.caption("Enter how much goes to each G/L - the amounts must add up to the total - or leave "
                               "every amount blank to split the total evenly. Each G/L becomes its own line on the "
                               "invoice, and the Requisition Form shows G/L MULTIPLE with the split next to it.")
                description = st.text_area("Description (optional)", max_chars=MAX_DESCRIPTION, height=90,
                                           key=f"ra_desc_{token}",
                                           placeholder="Explain the invoice if needed, e.g. what it's for or why it's split.",
                                           help="Printed in the Description section of the Requisition Form.")
                go = st.form_submit_button("Submit", type="primary", width="stretch")

        if not go:
            return
        signed_at = datetime.now(timezone.utc)              # the moment Submit was pressed
        if not stage2:
            amount = _parse_amount(amount_txt)
            if amount is None or amount == 0:
                st.error("Enter the amount to pay, e.g. 1234.56.")
                return
            gls = [UNKNOWN if unk else g for g, unk in zip(gl_txt, unknown)]
            missing = [i + 1 for i, g in enumerate(gls) if g != UNKNOWN and not GL_FORMAT.match(g)]
            if missing:
                which = "" if len(gls) == 1 else f" for G/L account {', '.join(map(str, missing))}"
                st.error(f"Enter the G/L as xxxxx-yy-zzzz{which}, e.g. 50200-01-1000 (numbers and dashes only), "
                         "or tick \"Don't know the G/L\".")
                return
            amounts = None
            if len(gls) > 1 and any(t.strip() for t in amt_txt):
                amounts = [_parse_amount(t) for t in amt_txt]
            try:
                check_split(amount, gls, amounts)
            except ValueError as e:
                st.error(_md(str(e)))
                return
        try:
            with st.spinner("Signing the Requisition Form in Business Central..."):
                msg = submit(req, amount, gls, user_email, signed_at, description, amounts, on_behalf=on_behalf)
        except (BCError, ValueError) as e:
            st.error(str(e))
            return
        _notify_creator(req, user_email, on_behalf, signed_at, stage2, amount, gls)
        st.cache_data.clear()
        st.session_state[f"ra_msg_{token}"] = msg
        st.rerun()


def _notify_creator(req, signed_by, on_behalf, signed_at, stage2, amount, gls) -> None:
    """Tell the BC user who sent it for approval that it was approved (never fails the submit)."""
    from reqapproval import mailer

    who = f"{signed_by} on behalf of {on_behalf}" if on_behalf else signed_by
    vendor = req["vendor_name"] or ""
    distinct = {g for g in gls if g}
    gl = ("Not coded" if distinct == {UNKNOWN} else distinct.pop()) if len(distinct) == 1 else ("MULTIPLE" if distinct else "")
    text = (
        f"{who} approved the requisition for purchase invoice {req['invoice_no']}"
        + (f" ({vendor})" if vendor else "") + f" at {_central(signed_at)}.\n\n"
        + ("This was the 2nd approval.\n" if stage2 else "")
        + (f"Amount - {_money(amount)}\n" if amount is not None else "")
        + (f"G/L - {gl}\n" if gl else "")
        + "\nThe signed Requisition Form is on the purchase invoice in Business Central.\n\n" + mailer.FOOTER)
    mailer.notify(req["creator_email"] or "", f"Requisition approved: {vendor} - Invoice #{req['invoice_no']}", text)


def render_list(user_email: str, is_admin: bool = False) -> None:
    """Optional: open approvals for the signed-in user (for a sidebar or the landing page)."""
    rows = db.open_for(user_email, include_all=is_admin, also=principals_for(user_email))
    if not rows:
        return
    st.subheader("Requisition approvals waiting for you")
    for r in rows:
        st.markdown(f"- [{r['invoice_no']} - {r['vendor_name'] or ''}]({portal_url(r['token'])})"
                    + (" (2nd approval)" if r["stage"] == 2 else "")
                    + (f" - for {acting_for(user_email, r['approver_email'])}" if acting_for(user_email, r["approver_email"]) else ""))
