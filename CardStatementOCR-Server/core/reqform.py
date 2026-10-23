"""
Requisition Form signing (card, benefits and requisition-approval submits).

BC attaches "RequisitionForm-<invoice no>" to the purchase invoice when the statement is
sent. On Submit, the portal downloads it, fills in Cost / G/L Account / Approved By, and
uploads it back as "RequisitionForm-<invoice no>-signed.pdf" - same naming the
Requisition Approval flow uses. The blank original stays on the invoice.

Requisition approval with a 2nd approver: the 1st approver's signature goes on "Signed By:"
(sign_label) and the 2nd approver's on "Approved By:" (cost/G/L left as the 1st approver set them).

Requires: pip install pymupdf
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
import os
from zoneinfo import ZoneInfo

import pymupdf as fitz
import requests

from core.bc_client import BCClient, BCError, attach, log

CENTRAL = ZoneInfo(os.environ.get("OCR_TIMEZONE", "America/Chicago"))   # time shown in signatures
SIGNED_BY = "Signed By:"
APPROVED_BY = "Approved By:"


# ------------------------------------------------------------------ the PDF
def _money(amount: float) -> str:
    return f"{'-' if amount < 0 else ''}${abs(amount):,.2f}"


def _central(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(CENTRAL)
    # 10/08/2026 2:35:12 PM CDT  (CST in winter)
    return f"{when:%m/%d/%Y} {when.hour % 12 or 12}:{when:%M:%S %p} {when.tzname()}"


def _label(page: fitz.Page, text: str) -> tuple[fitz.Rect, float]:
    """Box of the label and its text baseline."""
    hits = page.search_for(text)
    if not hits:
        raise ValueError(f"'{text}' not found on the requisition form")
    rect = hits[0]
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip().startswith(text) and fitz.Rect(span["bbox"]).intersects(rect):
                    return rect, span["origin"][1]
    return rect, rect.y1 - rect.height * 0.22


def _write_after(page: fitz.Page, found: tuple[fitz.Rect, float], text: str, font: str, size: float) -> float:
    """Write text on the underline to the right of a label (same row).
    Returns the right edge of whichever is further right: the text written or its underline."""
    label, baseline = found
    x, x_end = None, None
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]
                if abs(a.y - b.y) > 1:
                    continue
                y, x0, x1 = a.y, min(a.x, b.x), max(a.x, b.x)
            elif item[0] == "re" and item[1].height < 2:
                y, x0, x1 = item[1].y0, item[1].x0, item[1].x1
            else:
                continue
            if label.y1 - 3 <= y <= label.y1 + 8 and x0 >= label.x1 - 1:
                if x is None or x0 < x:
                    x, x_end = x0, x1
    x = (x + 2) if x is not None else label.x1 + 8

    room = page.rect.width - 36 - x          # shrink to fit the page if long (e.g. a long email)
    while size > 6 and fitz.get_text_length(text, fontname=font, fontsize=size) > room:
        size -= 0.5
    page.insert_text((x, baseline), text, fontname=font, fontsize=size, color=(0, 0, 0))
    return max(x + fitz.get_text_length(text, fontname=font, fontsize=size), x_end or 0)


def _underlines_between(page: fitz.Page, y_top: float, y_bottom: float) -> list[tuple[float, float, float]]:
    """Horizontal rules (y, x0, x1) strictly between two y positions, top to bottom."""
    found = {}
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l" and abs(item[1].y - item[2].y) <= 1:
                y, x0, x1 = item[1].y, min(item[1].x, item[2].x), max(item[1].x, item[2].x)
            elif item[0] == "re" and item[1].height < 2:
                y, x0, x1 = item[1].y0, item[1].x0, item[1].x1
            else:
                continue
            if y_top < y < y_bottom and x1 - x0 > 100:
                found[round(y)] = (y, x0, x1)
    return [found[k] for k in sorted(found)]


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Word-wrap text to a width; newlines the user typed start a new line."""
    out = []
    for para in (text or "").splitlines() or [""]:
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}" if cur else word
            if cur and fitz.get_text_length(trial, fontname=font, fontsize=size) > width:
                out.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            out.append(cur)
    return out


def _write_description(page: fitz.Page, text: str, font: str = "helv", size: float = 10) -> None:
    """The approver's description, word-wrapped onto the Description underlines (smaller type if it's long)."""
    top, _ = _label(page, "Description:")
    bottom, _ = _label(page, "G/L Account:")
    rules = _underlines_between(page, top.y0 - 2, bottom.y0)
    if not rules:                                   # no rules found: one line to the right of the label
        _write_after(page, (top, top.y1 - top.height * 0.22), " ".join(text.split()), font, size)
        return
    width = min(x1 - x0 for _, x0, x1 in rules) - 4
    while True:
        lines = _wrap(text, font, size, width)
        if len(lines) <= len(rules) or size <= 7:
            break
        size -= 0.5
    if len(lines) > len(rules):                     # still too long: cut the last line short
        last = " ".join(lines[len(rules) - 1:])
        while last and fitz.get_text_length(last + "...", fontname=font, fontsize=size) > width:
            last = last[:-1]
        lines = lines[:len(rules) - 1] + [last.rstrip() + "..."]
    for (y, x0, _), line in zip(rules, lines):
        page.insert_text((x0 + 2, y - 3), line, fontname=font, fontsize=size, color=(0, 0, 0))


def _write_gl_splits(page: fitz.Page, gl_found: tuple[fitz.Rect, float], x_start: float, entries: list[str],
                     font: str = "helv", size: float = 9) -> None:
    """The G/L split ("50200-01-1000  $333.34") in the white space to the right of the G/L Account line:
    rows from the G/L line down to just above Cost, then more columns to the right."""
    label, baseline = gl_found
    cost, _ = _label(page, "Cost:")
    x_start += 14
    right = page.rect.width - 36
    while True:
        lead = size + 2
        rows = max(1, int((cost.y0 - 4 - (baseline - size)) // lead))
        cols = -(-len(entries) // rows)
        col_w = max(fitz.get_text_length(e, fontname=font, fontsize=size) for e in entries) + 16
        if x_start + cols * col_w - 16 <= right or size <= 6:
            break
        size -= 0.5
    for i, text in enumerate(entries):
        r, c = i % rows, i // rows
        page.insert_text((x_start + c * col_w, baseline + r * lead), text, fontname=font, fontsize=size, color=(0, 0, 0))


def signature_text(signed_by: str, signed_at: datetime, on_behalf: str | None = None) -> str:
    """digitally signed by <email> [on behalf of <name>] at <local time (OCR_TIMEZONE)>"""
    return (f"digitally signed by {signed_by}" + (f" on behalf of {on_behalf}" if on_behalf else "")
            + f" at {_central(signed_at)}")


def stamp_requisition_form(pdf: bytes, cost: float | None, gl_account: str, signed_by: str, signed_at: datetime,
                           sign_label: str = APPROVED_BY, description: str | None = None,
                           gl_splits: list[str] | None = None, on_behalf: str | None = None) -> bytes:
    """cost=None leaves Cost alone (2nd approver signing a form the 1st approver already filled).
    description: the approver's explanation, written in the Description section.
    gl_splits: with G/L Account MULTIPLE, each G/L and its share, written to the right of the G/L line."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    page = doc[0]
    if cost is not None:
        _write_after(page, _label(page, "Cost:"), _money(cost), "hebo", 12)
    if gl_account:
        gl_found = _label(page, "G/L Account:")
        gl_end = _write_after(page, gl_found, gl_account, "hebo", 12)
        if gl_splits:
            _write_gl_splits(page, gl_found, gl_end, gl_splits)
    if description and description.strip():
        _write_description(page, description.strip())
    _write_after(page, _label(page, sign_label), signature_text(signed_by, signed_at, on_behalf), "helv", 10)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out


# ------------------------------------------------------------------ BC round trip
def _stem(file_name: str) -> str:
    return file_name.rsplit(".", 1)[0] if "." in file_name else file_name


def _download(bc: BCClient, url: str) -> bytes:
    for attempt in range(6):
        resp = requests.get(url, timeout=120, headers={"Authorization": f"Bearer {bc.token}"})
        if resp.status_code not in (429, 503):
            break
        time.sleep(float(resp.headers.get("Retry-After") or 0) or min(2 ** attempt, 30))
    if resp.status_code != 200:
        raise BCError(f"Download requisition form: {resp.status_code} {resp.text[:300]}")
    return resp.content


NOT_CODED = "Not coded"   # how a charge marked "Don't know the G/L" (stored as UNKNOWN) appears on the form


def gl_summary(gl_amounts: list[tuple[str, float]]) -> tuple[str, list[str]]:
    """(G/L field, split lines) for the form from (G/L string, amount) pairs.
    One G/L string -> that string and no split lines; several -> MULTIPLE and one line per G/L with its total."""
    totals: dict[str, float] = {}
    for gl, amount in gl_amounts:
        gl = (gl or "").strip()
        gl = NOT_CODED if gl.upper() == "UNKNOWN" else gl
        if gl:
            totals[gl] = round(totals.get(gl, 0.0) + amount, 2)
    if len(totals) == 1:
        return next(iter(totals)), []
    if not totals:
        return "", []
    coded = sorted(g for g in totals if g != NOT_CODED) + ([NOT_CODED] if NOT_CODED in totals else [])
    return "MULTIPLE", [f"{g}  {_money(totals[g])}" for g in coded]


def sign_requisition_form(bc: BCClient, inv_url: str, invoice_no: str, amounts: list[float],
                          gls: list[str], signed_by: str, signed_at: datetime,
                          sign_label: str = APPROVED_BY, on_behalf: str | None = None,
                          gl_amounts: list[tuple[str, float]] | None = None) -> None:
    """Cost = total of every amount; G/L = the one G/L string if they're all the same, else MULTIPLE.
    gl_amounts: (G/L string, amount) per invoice line; with it, MULTIPLE lists each G/L and its total.
    sign_label: "Approved By:", or "Signed By:" when a 2nd approver signs "Approved By:" later.
    Safe to call again on a resubmit: does nothing once the -signed copy is on the invoice."""
    blank = f"RequisitionForm-{invoice_no}"
    signed = f"{blank}-signed"
    atts = bc.call("GET", f"{inv_url}/documentAttachments", "Read invoice attachments").get("value", [])
    if any(_stem(a.get("fileName", "")) == signed for a in atts):
        log.info("requisition form for %s already signed", invoice_no)
        return
    form = next((a for a in atts if _stem(a.get("fileName", "")) == blank), None)
    if not form:
        log.warning("no %s on invoice %s - requisition form not signed", blank, invoice_no)
        return                                   # sent before BC attached the form; don't block the submit

    if gl_amounts:
        gl, splits = gl_summary(gl_amounts)
    else:
        gl, splits = gl_summary([(g, 0.0) for g in gls])
        splits = []                                  # no amounts to show
    total = round(sum(amounts), 2)

    pdf = _download(bc, f"{inv_url}/documentAttachments({form['id']})/attachmentContent")
    try:
        stamped = stamp_requisition_form(pdf, total, gl, signed_by, signed_at, sign_label=sign_label,
                                         gl_splits=splits or None, on_behalf=on_behalf)
    except ValueError as e:
        raise BCError(f"Sign requisition form: {e}") from e
    attach(bc, inv_url, f"{signed}.pdf", stamped, "signed requisition form")
    log.info("signed %s (%s): cost %s, G/L %s, by %s", blank, sign_label, _money(total), gl, signed_by)
