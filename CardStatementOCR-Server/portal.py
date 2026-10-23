"""
Streamlit portal entry point: Microsoft 365 sign-in, then navigation across modules.
  * Invoice Receipts       - card statements (cards/pages.py)
  * Requisition Approvals  - every open 1st and 2nd approval in one dropdown (reqapproval/page.py);
                             emailed links (?r=<token>) open here with that invoice selected
"""
from __future__ import annotations

import streamlit as st

from cards import pages as cards_pages
from cards import store as cards_store
from core import settings as config
from reqapproval import page as reqapproval_page
from reqapproval import store as reqapproval_store

st.set_page_config(page_title="Invoice Receipts", page_icon="🧾", layout="wide")
cards_store.init()
reqapproval_store.init()


# ------------------------------------------------------------------ identity
def signed_in_email() -> str | None:
    if config.DEV_USER:
        return config.DEV_USER.lower()
    if not st.user.is_logged_in:
        return None
    info = st.user.to_dict()
    return (info.get("preferred_username") or info.get("email") or "").lower() or None


EMAIL = signed_in_email()
if not EMAIL:
    st.title("🧾 Invoice Receipts")
    st.write("Sign in with your company Microsoft 365 account to see what's waiting on you.")
    if st.button("Sign in with Microsoft", type="primary"):
        st.login("microsoft")
    st.stop()

IS_ADMIN = EMAIL in config.ADMINS
st.session_state["_ocr_email"] = EMAIL
st.session_state["_ocr_admin"] = IS_ADMIN
with st.sidebar:
    st.caption(f"Signed in as **{EMAIL}**")
    st.caption(f"[Source code]({config.SOURCE_URL}) · AGPL-3.0")
    if not config.DEV_USER and st.button("Sign out"):
        st.logout()


# ------------------------------------------------------------------ navigation
# A Requisition Approval link (?r=...) opens the Requisition Approvals page; everything else
# starts on Invoice Receipts.
approval_link = bool(st.query_params.get("r"))
pages = [st.Page(cards_pages.receipts_page, title="Invoice Receipts", icon="🧾", default=not approval_link),
         st.Page(reqapproval_page.approvals_page, title="Requisition Approvals", icon="✅",
                 url_path="requisitions", default=approval_link)]
if IS_ADMIN:
    pages.append(st.Page(cards_pages.breakdown_page, title="Statement breakdown", icon="💳", url_path="breakdown"))
st.navigation(pages).run()
