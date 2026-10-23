"""SMTP for the requisition-approval emails (1st and 2nd approver).
Settings come from config/ocr.env: SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASS,
and optionally OCR_AP_EMAIL / OCR_IT_EMAIL for the footer."""
from __future__ import annotations

import html
import os
import smtplib
from email.message import EmailMessage

import core.settings  # noqa: F401 - loads config/ocr.env before FOOTER is built

# To-address -> people always CC'd on emails to them. Empty: no automatic CCs.
EXTRA_CC: dict[str, list[str]] = {}

FOOTER = "".join(line for line in (
    f"For any invoicing issues, please contact Accounts Payable at {os.environ['OCR_AP_EMAIL']}.\n" if os.environ.get("OCR_AP_EMAIL") else "",
    f"For any technical issues, please contact IT at {os.environ['OCR_IT_EMAIL']}.\n" if os.environ.get("OCR_IT_EMAIL") else ""))


def notify(to_addr: str, subject: str, text: str, cc: list[str] | None = None) -> bool:
    """An informational email (no portal link). Never raises: the work it reports is already done,
    so a mail problem is logged and reported back as False."""
    import logging
    if not (to_addr or "").strip():
        return False
    if not (os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS")):
        logging.getLogger("ocr").warning("SMTP not configured; '%s' not emailed to %s", subject, to_addr)
        return False
    try:
        send(to_addr, subject, text, "", cc=cc, extra_cc=False)
        return True
    except Exception as e:
        logging.getLogger("ocr").error("email '%s' to %s failed: %s", subject, to_addr, e)
        return False


def send(to_addr: str, subject: str, text: str, link: str, cc: list[str] | None = None,
         extra_cc: bool = True) -> None:
    """Plain-text body with the link on its own line, plus an HTML copy where the link is clickable.
    extra_cc: add the EXTRA_CC people for this recipient (2nd-approver emails)."""
    to_addr = to_addr.strip()
    extra = EXTRA_CC.get(to_addr.lower(), []) if extra_cc else []
    ccs = [x.strip() for x in (cc or []) + extra if x and x.strip()]
    ccs = [x for x in dict.fromkeys(ccs) if x.lower() != to_addr.lower()]

    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = to_addr
    if ccs:
        msg["Cc"] = ", ".join(ccs)
    msg["Subject"] = subject
    msg.set_content(text)
    body = html.escape(text)
    if link:
        body = body.replace(html.escape(link), f'<a href="{html.escape(link)}">Open the approval portal</a>')
    msg.add_alternative(f"<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
                        f"{body.replace(chr(10), '<br>')}</div>", subtype="html")

    with smtplib.SMTP(os.environ.get("SMTP_SERVER", "smtp.office365.com"), int(os.environ.get("SMTP_PORT", "587"))) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        smtp.send_message(msg)
