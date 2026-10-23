"""Who is signed in to the portal (set once per run by portal.py, read by every module's pages)."""
import streamlit as st


def current_email() -> str:
    return st.session_state.get("_ocr_email", "")


def is_admin() -> bool:
    return bool(st.session_state.get("_ocr_admin", False))
