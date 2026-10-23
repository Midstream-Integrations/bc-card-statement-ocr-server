"""Email for the requisition-approval flow (1st and 2nd approver, and "Approved" notices).

Two ways to send, picked by MAIL_METHOD in config/ocr.env:

  graph (default)  Microsoft Graph sendMail as the Entra app (client credentials, no mailbox
                   password). Uses BC_TENANT_ID / BC_CLIENT_ID / BC_CLIENT_SECRET, or MAIL_TENANT_ID /
                   MAIL_CLIENT_ID / MAIL_CLIENT_SECRET to send from a separate app. MAIL_FROM is the
                   mailbox it sends as. The app needs the Graph application permission Mail.Send
                   (ideally scoped to MAIL_FROM with Exchange RBAC for Applications; see docs/INSTALL.md 6.3).
  smtp             SMTP with STARTTLS and a user name/password: SMTP_SERVER, SMTP_PORT, SMTP_USER,
                   SMTP_PASS. For non-Microsoft providers. Microsoft 365 turns off password (Basic)
                   auth for SMTP AUTH by default from the end of December 2026, so use graph there.
                   If MAIL_METHOD isn't set, a config with SMTP_USER/SMTP_PASS and no MAIL_FROM uses smtp.

Optionally OCR_AP_EMAIL / OCR_IT_EMAIL for the footer.

Test from /opt/ocr:  .venv/bin/python -m reqapproval.mailer you@example.com
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
import threading
import time
from email.message import EmailMessage

import requests

import core.settings  # noqa: F401 - loads config/ocr.env before anything below reads it

log = logging.getLogger("ocr")

# To-address -> people always CC'd on emails to them. Empty: no automatic CCs.
EXTRA_CC: dict[str, list[str]] = {}

FOOTER = "".join(line for line in (
    f"For any invoicing issues, please contact Accounts Payable at {os.environ['OCR_AP_EMAIL']}.\n" if os.environ.get("OCR_AP_EMAIL") else "",
    f"For any technical issues, please contact IT at {os.environ['OCR_IT_EMAIL']}.\n" if os.environ.get("OCR_IT_EMAIL") else ""))


class MailError(Exception):
    pass


def _env(name: str, fallback: str = "") -> str:
    return (os.environ.get(name) or (os.environ.get(fallback) if fallback else "") or "").strip()


def method() -> str:
    m = _env("MAIL_METHOD").lower()
    if m in ("graph", "smtp"):
        return m
    # Not set: an existing SMTP-only config keeps working until it's switched over.
    if not _env("MAIL_FROM") and _env("SMTP_USER") and _env("SMTP_PASS"):
        return "smtp"
    return "graph"


def sender() -> str:
    return _env("MAIL_FROM", "SMTP_USER") if method() == "graph" else _env("SMTP_USER")


def configured() -> bool:
    if method() == "smtp":
        return bool(_env("SMTP_SERVER") and _env("SMTP_USER") and _env("SMTP_PASS"))
    return bool(sender() and _env("MAIL_TENANT_ID", "BC_TENANT_ID") and _env("MAIL_CLIENT_ID", "BC_CLIENT_ID")
                and _env("MAIL_CLIENT_SECRET", "BC_CLIENT_SECRET"))


def notify(to_addr: str, subject: str, text: str, cc: list[str] | None = None) -> bool:
    """An informational email (no portal link). Never raises: the work it reports is already done,
    so a mail problem is logged and reported back as False."""
    if not (to_addr or "").strip():
        return False
    if not configured():
        log.warning("Email not configured (MAIL_METHOD=%s); '%s' not emailed to %s", method(), subject, to_addr)
        return False
    try:
        send(to_addr, subject, text, "", cc=cc, extra_cc=False)
        return True
    except Exception as e:
        log.error("email '%s' to %s failed: %s", subject, to_addr, e)
        return False


def send(to_addr: str, subject: str, text: str, link: str, cc: list[str] | None = None,
         extra_cc: bool = True) -> None:
    """Plain-text body with the link on its own line, plus an HTML copy where the link is clickable.
    extra_cc: add the EXTRA_CC people for this recipient (2nd-approver emails). Raises on failure."""
    to_addr = to_addr.strip()
    extra = EXTRA_CC.get(to_addr.lower(), []) if extra_cc else []
    ccs = [x.strip() for x in (cc or []) + extra if x and x.strip()]
    ccs = [x for x in dict.fromkeys(ccs) if x.lower() != to_addr.lower()]

    body = html.escape(text)
    if link:
        body = body.replace(html.escape(link), f'<a href="{html.escape(link)}">Open the approval portal</a>')
    html_body = (f"<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
                 f"{body.replace(chr(10), '<br>')}</div>")

    if not configured():
        raise MailError(f"Email is not configured (MAIL_METHOD={method()}); see config/ocr.env")
    if method() == "smtp":
        _send_smtp(to_addr, ccs, subject, text, html_body)
    else:
        _send_graph(to_addr, ccs, subject, html_body)


# ---- Microsoft Graph
_token_lock = threading.Lock()
_token_cache: dict[str, float | str] = {"token": "", "expires": 0.0}


def _graph_token() -> str:
    with _token_lock:
        if _token_cache["token"] and time.time() < float(_token_cache["expires"]) - 300:
            return str(_token_cache["token"])
        tenant = _env("MAIL_TENANT_ID", "BC_TENANT_ID")
        resp = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials",
                  "client_id": _env("MAIL_CLIENT_ID", "BC_CLIENT_ID"),
                  "client_secret": _env("MAIL_CLIENT_SECRET", "BC_CLIENT_SECRET"),
                  "scope": "https://graph.microsoft.com/.default"},
            timeout=30)
        if resp.status_code != 200:
            raise MailError(f"Could not get a Microsoft Graph token: {resp.status_code} {resp.text[:300]}")
        j = resp.json()
        _token_cache["token"] = j["access_token"]
        _token_cache["expires"] = time.time() + int(j.get("expires_in", 3600))
        return j["access_token"]


def _send_graph(to_addr: str, ccs: list[str], subject: str, html_body: str) -> None:
    frm = sender()
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to_addr}}],
            "ccRecipients": [{"emailAddress": {"address": c}} for c in ccs],
        },
        "saveToSentItems": True,
    }
    url = f"https://graph.microsoft.com/v1.0/users/{requests.utils.quote(frm)}/sendMail"
    for attempt in range(5):                            # Graph throttles with 429 / 503: wait and retry
        resp = requests.post(url, json=payload, timeout=60,
                             headers={"Authorization": f"Bearer {_graph_token()}"})
        if resp.status_code not in (429, 503, 504):
            break
        wait = float(resp.headers.get("Retry-After") or 0) or min(2 ** attempt, 30)
        log.info("Graph sendMail throttled (%s); retrying in %.0fs", resp.status_code, wait)
        time.sleep(wait)
    if resp.status_code == 202:
        return
    try:
        err = resp.json()["error"]
        detail = f"{err.get('code')}: {err.get('message')}"
    except Exception:
        detail = resp.text[:300]
    hint = ""
    if resp.status_code == 403:
        hint = (f" (the Entra app isn't allowed to send as {frm}: add the Graph application permission "
                "Mail.Send, or check its Exchange RBAC scope; see docs/INSTALL.md 6.3)")
    elif resp.status_code == 404:
        hint = f" ({frm} isn't a mailbox in this tenant; check MAIL_FROM)"
    raise MailError(f"Graph sendMail as {frm} failed: {resp.status_code} {detail}{hint}")


# ---- SMTP (non-Microsoft providers)
def _send_smtp(to_addr: str, ccs: list[str], subject: str, text: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["From"] = _env("SMTP_USER")
    msg["To"] = to_addr
    if ccs:
        msg["Cc"] = ", ".join(ccs)
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(_env("SMTP_SERVER"), int(_env("SMTP_PORT") or "587")) as smtp:
        smtp.starttls()
        smtp.login(_env("SMTP_USER"), _env("SMTP_PASS"))
        smtp.send_message(msg)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: python -m reqapproval.mailer <to-address>")
    print(f"Sending a test email to {sys.argv[1]} via {method()} as {sender() or '(not set)'} ...")
    send(sys.argv[1], "Card Statement OCR test email",
         "This is a test email from the Card Statement OCR server.\n\n" + FOOTER, "")
    print("Sent.")
