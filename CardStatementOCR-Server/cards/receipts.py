"""
Receipt check, mobile-deposit style: OCR the receipt, find its total and date, compare to the card charge.

Only used when OCR_READ_RECEIPTS is on. When it's off, an uploaded receipt is stored with the
status "attached" (accepted without being read).

Statuses
  attached       receipt reading is turned off                       -> accepted
  match          charge amount on a TOTAL-type line, date OK         -> accepted
  found          charge amount on the receipt (unlabeled), date OK   -> accepted, flagged in BC
  date_mismatch  amount OK but the receipt date is too far from the charge date -> user must explain
  mismatch       amount not found (tip added, partial receipt, bad photo)       -> user must explain
  unreadable     no amounts found at all, or the read took longer than OCR_TIMEOUT_SECONDS
                                                                                 -> user must explain or retake
"Date OK" = within DATE_WINDOW days of the transaction date, or no date could be read (shown to the user).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date

from core.settings import RECEIPT_DATE_WINDOW as DATE_WINDOW   # days either side of the charge

AMOUNT = re.compile(r"(?<![\d.,])\$?\s?(\d{1,3}(?:,\d{3})+|\d+)[.,](\d{2})(?![\d])")
TOTAL_WORDS = re.compile(r"\b(GRAND\s*TOTAL|TOTAL|AMOUNT\s*DUE|BALANCE\s*DUE|AMOUNT\s*PAID|AMOUNT|CHARGED?|PAID)\b", re.I)
NOT_TOTAL = re.compile(r"SUB\s*-?\s*TOTAL|TOTAL\s*(SAVINGS|SAVED|ITEMS?|QTY|TAX|DISCOUNT)|\bTAX\b|\bTIP\b|CHANGE|CASH", re.I)

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun",
                                        "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
DATE_PATTERNS = [
    (re.compile(r"(?<![\d/.-])(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})(?![\d/.-])"), "mdy"),  # 09/14/2026, 9-14-26
    (re.compile(r"(?<![\d/-])(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?![\d/-])"), "ymd"),           # 2026-09-14
    (re.compile(rf"\b{_MON}\s+(\d{{1,2}}),?\s+(\d{{4}})", re.I), "bdy"),                       # Sep 14, 2026
    (re.compile(rf"\b(\d{{1,2}})[\s-]{_MON}[\s,\-]*(\d{{4}}|\d{{2}})\b", re.I), "dby"),         # 14 Sep 2026
]


@dataclass
class ReceiptCheck:
    status: str                       # see module docstring
    detected_total: float | None      # best guess at the receipt total
    amounts: list[float]
    detected_date: date | None = None # receipt date closest to the charge date

    @property
    def accepted(self) -> bool:
        return self.status in ("match", "found")


def _amounts(line: str) -> list[float]:
    return [float(a.replace(",", "") + "." + c) for a, c in AMOUNT.findall(line)]


def _year(y: str) -> int:
    return int(y) + 2000 if len(y) == 2 else int(y)


def find_dates(lines: list[str]) -> list[date]:
    found = []
    for line in lines:
        for rx, kind in DATE_PATTERNS:
            for m in rx.finditer(line):
                g = m.groups()
                try:
                    if kind == "mdy":
                        d = date(_year(g[2]), int(g[0]), int(g[1]))
                    elif kind == "ymd":
                        d = date(int(g[0]), int(g[1]), int(g[2]))
                    elif kind == "bdy":
                        d = date(int(g[2]), _MONTHS[g[0][:3].lower()], int(g[1]))
                    else:
                        d = date(_year(g[2]), _MONTHS[g[1][:3].lower()], int(g[0]))
                except (ValueError, KeyError):
                    continue
                if 2000 <= d.year <= 2100:
                    found.append(d)
    return found


def check_lines(lines: list[str], expected: float, tran_date: date | None = None) -> ReceiptCheck:
    expected = round(abs(expected), 2)
    all_amounts, labeled = [], []
    for i, line in enumerate(lines):
        vals = _amounts(line)
        all_amounts += vals
        if TOTAL_WORDS.search(line) and not NOT_TOTAL.search(line):
            # the value is usually on the same line; sometimes on the next one
            labeled += vals or (_amounts(lines[i + 1]) if i + 1 < len(lines) else [])

    dates = find_dates(lines)
    closest = min(dates, key=lambda d: abs((d - tran_date).days)) if dates and tran_date else (dates[0] if dates else None)
    date_ok = closest is None or tran_date is None or abs((closest - tran_date).days) <= DATE_WINDOW

    if not all_amounts:
        return ReceiptCheck("unreadable", None, [], closest)
    detected = max(labeled) if labeled else max(all_amounts)
    if any(abs(v - expected) < 0.005 for v in labeled):
        status = "match"
    elif any(abs(v - expected) < 0.005 for v in all_amounts):
        status = "found"
    else:
        return ReceiptCheck("mismatch", detected, all_amounts, closest)
    return ReceiptCheck(status if date_ok else "date_mismatch", expected, all_amounts, closest)


RECEIPT_PAGES = 3   # a receipt is a page or two; reading more only makes a wrong upload (a whole statement) slow


def _pdf_lines(data: bytes) -> list[str]:
    """Text of the first RECEIPT_PAGES pages: the PDF's own text when it has some (fast, exact),
    otherwise docTR OCR of just those pages."""
    import pymupdf as fitz

    from core import ocr

    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.page_count == 0:
            raise ValueError("the PDF has no pages")
        n = min(doc.page_count, RECEIPT_PAGES)
        text = [l for i in range(n) for l in doc[i].get_text().splitlines() if l.strip()]
        if text:
            return text
        if doc.page_count > n:
            doc.select(list(range(n)))
        first = doc.tobytes()
    return [l for p in ocr.lines_from_pdf(first, scale=2.0) for l in p]


def prepare_receipt(data: bytes, filename: str) -> tuple[bytes, str]:
    """File bytes to store (and read), and the stored filename.
    Photos (incl. iPhone HEIC) become JPEG, max 2000px, so BC attachments stay small and viewable,
    and docTR isn't handed a full-size phone photo."""
    if filename.lower().endswith(".pdf"):
        return data, filename

    from PIL import Image

    from core import ocr

    img = Image.fromarray(ocr.load_image(data))
    img.thumbnail((2000, 2000))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    stem = filename.rsplit(".", 1)[0]
    return buf.getvalue(), f"{stem}.jpg"


def receipt_lines(stored: bytes, name: str) -> list[str]:
    """OCR a file returned by prepare_receipt. Runs in the OCR worker process (core.ocr_worker)."""
    if name.lower().endswith(".pdf"):
        return _pdf_lines(stored)

    from core import ocr

    return ocr.lines_from_images([ocr.load_image(stored)])[0]


def read_receipt(data: bytes, filename: str) -> tuple[list[str], bytes, str]:
    """OCR a receipt in this process, with no time limit. Returns (text lines, file bytes to store,
    stored filename). The portal uses prepare_receipt + receipt_lines through core.ocr_worker instead."""
    stored, name = prepare_receipt(data, filename)
    return receipt_lines(stored, name), stored, name
