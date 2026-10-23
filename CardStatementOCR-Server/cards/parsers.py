"""
Statement parsers, one per card issuer ("merchant" in the UI).

To add an issuer:
  1. write parse_<issuer>(pages) -> Statement, where pages is a list of pages, each a list of text lines
  2. register it in MERCHANTS at the bottom (id is what BC sends; keep it lowercase, no spaces)
  3. nothing else: BC sends merchant "auto" by default, and the server picks the parser whose
     looks_like() matches the PDF. A BC enum value for a specific issuer is optional; its server id
     goes in Codeunit "MID Card Stmt Mgt".MerchantId.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd

TX_COLUMNS = ["name", "last4", "type", "post_date", "tran_date", "description",
              "reference", "amount", "mcc", "merchant_zip", "details", "page"]
CH_COLUMNS = ["name", "last4", "role", "limit", "statement_total"]


@dataclass
class Statement:
    merchant: str
    closing_date: date | None
    cardholders: pd.DataFrame   # name, last4, role, limit, statement_total
    transactions: pd.DataFrame  # TX_COLUMNS; purchases positive, payments/credits negative
    method: str = ""
    warnings: list[str] = field(default_factory=list)

    def for_holder(self, last4: str) -> pd.DataFrame:
        return self.transactions[self.transactions["last4"] == last4].reset_index(drop=True)


@dataclass(frozen=True)
class Merchant:
    id: str
    label: str
    parse: Callable[[list[list[str]]], Statement]
    looks_like: Callable[[list[list[str]]], bool]   # does this text belong to this issuer's format?


def money(s: str) -> float:
    neg = "-" in s
    v = float(re.sub(r"[^\d.]", "", s))
    return -v if neg else v


def to_date(mmdd: str, closing: date | None) -> date | None:
    mm, dd = (int(x) for x in mmdd.split("/"))
    if closing is None:
        return None
    year = closing.year - 1 if mm > closing.month else closing.year  # Dec charges on a Jan statement
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


# ============================================================ Huntington
_MONEY = r"-?[$S]?-?\d{1,3}(?:,\d{3})*\.\d{2}"
_CARD = r"[X×x]{4}\s*[X×x]{4}\s*[X×x]{4}\s*(?P<last4>\d{4})"
_H_CLOSING = re.compile(r"Closing Date\s+(?P<m>\d{2})/(?P<d>\d{2})/(?P<y>\d{2,4})", re.I)
_H_SUMMARY = re.compile(rf"^(?P<name>[A-Z][A-Z0-9 ,.'&\-]+?)\s+{_CARD}\s+(?P<limit>{_MONEY})\s+(?P<activity>{_MONEY})")
_H_HOLDER = re.compile(rf"^(?P<name>[A-Z][A-Z0-9 ,.'&\-]+?)\s+{_CARD}(?:\s+Total Activity)?\s+(?P<amt>{_MONEY})\s*$", re.I)
_H_TXN = re.compile(rf"^(?P<post>\d{{2}}/\d{{2}})\s+(?P<tran>\d{{2}}/\d{{2}})\s+(?P<desc>.+?)\s+(?P<ref>\S+)\s+(?P<amt>{_MONEY})\s*$")
_H_MCC = re.compile(r"^Mcc:?\s*(?P<mcc>\d{4})?\s*Merchant Zip:?\s*(?P<zip>\d*)", re.I)
_H_NOISE = re.compile(
    r"^(Page \d+ of \d+|MASTERCARD BUSINESS CARD|Closing Date|NEW PURCHASES|"
    r"Post\s+Tran|Date\s+Date|Transactions Continued|\d{4}\s+XYZ|Reference|Number$)", re.I)


def huntington_looks_like(pages: list[list[str]]) -> bool:
    text = "\n".join(l.upper() for p in pages for l in p)
    return "CARDHOLDER SUMMARY" in text and "HUNTINGTON" in text


def parse_huntington(pages: list[list[str]]) -> Statement:
    closing, holders, txns, warnings = None, {}, [], []
    section = kind = current = last = None

    for page_no, lines in enumerate(pages, start=1):
        for raw in lines:
            line = " ".join(raw.split())
            if not line:
                continue
            up = line.upper()

            if closing is None and (m := _H_CLOSING.search(line)):
                y = int(m["y"]) + (2000 if int(m["y"]) < 100 else 0)
                closing = date(y, int(m["m"]), int(m["d"]))

            if up.startswith("CARDHOLDER SUMMARY"):
                section = "summary"; continue
            if up.startswith("PAYMENTS AND CREDITS"):
                section = "pc"; continue
            if up.startswith("NEW PURCHASES"):
                section, kind = "purchases", "Purchase"; continue
            if re.match(r"\d{4} TOTALS", up) or up.startswith("INTEREST CHARGE"):
                section = last = None; continue
            if section == "pc" and up.startswith("PAYMENTS"):
                kind = "Payment"; continue
            if section == "pc" and up.startswith("CREDITS"):
                kind = "Credit"; continue
            if section is None:
                continue

            if section == "summary":
                if m := _H_SUMMARY.match(line):
                    holders[m["last4"]] = dict(
                        name=m["name"].strip(), last4=m["last4"],
                        role="Administrator" if not holders else "Employee",
                        limit=money(m["limit"]), statement_total=money(m["activity"]))
                continue

            if m := _H_HOLDER.match(line):
                current, last = (m["name"].strip(), m["last4"]), None
                continue

            if m := _H_TXN.match(line):
                if current is None:
                    warnings.append(f"p{page_no}: transaction with no cardholder: {line}")
                    continue
                amt = abs(money(m["amt"])) * (-1 if kind in ("Payment", "Credit") else 1)
                last = dict(name=current[0], last4=current[1], type=kind,
                            post_date=to_date(m["post"], closing), tran_date=to_date(m["tran"], closing),
                            description=m["desc"].strip(), reference=m["ref"], amount=amt,
                            mcc="", merchant_zip="", details="", page=page_no)
                txns.append(last)
                continue

            if last is not None and not _H_NOISE.match(line):
                if m := _H_MCC.match(line):
                    last["mcc"], last["merchant_zip"] = m["mcc"] or "", m["zip"] or ""
                else:
                    last["details"] = (last["details"] + " | " + line).strip(" |")

    return _finish("huntington", closing, holders, txns, warnings)


# ============================================================ U.S. Bank
# Layout (Corporate/Community card): page 1 is the account summary, then one "Transactions <NAME>"
# block per cardholder. The card's last 4 digits only appear on that block's closing
# "Total for Account **** **** **** 1234 $x" line, so rows are buffered until then.
# Payments are in a "Transactions BILLING ACCOUNT ACTIVITY" block.
_U_PERIOD = re.compile(r"(?P<open>\d{2}/\d{2}/\d{4})\s*-\s*(?P<close>\d{2}/\d{2}/\d{4})")
_U_CLOSING = re.compile(r"Closing Date:?\s*(?P<close>\d{2}/\d{2}/\d{4})", re.I)
_U_HOLDER = re.compile(r"^Transactions\s+(?P<name>.+?)(?:\s+Credit Limit\s+(?P<limit>\$?[\d,]+(?:\.\d{2})?))?\s*$", re.I)
_U_TXN = re.compile(rf"^(?P<post>\d{{2}}/\d{{2}})\s+(?P<tran>\d{{2}}/\d{{2}})\s+(?P<ref>\S+)\s+(?P<desc>.+?)\s+"
                    rf"(?P<amt>{_MONEY})\s*(?P<cr>CR)?\s*$", re.I)
_U_TOTAL = re.compile(rf"^Total for Account\s*[*X×x\s]*(?P<last4>\d{{4}})\s+(?P<amt>{_MONEY})\s*(?P<cr>CR)?\s*$", re.I)


def usbank_looks_like(pages: list[list[str]]) -> bool:
    text = "\n".join(l.upper() for p in pages for l in p)
    return ("USBANK.COM" in text or "U.S. BANK" in text) and "TOTAL FOR ACCOUNT" in text


def _us_date(s: str) -> date:
    m, d, y = (int(x) for x in s.split("/"))
    return date(y, m, d)


def _us_name(raw: str) -> str:
    """'DOE,JANE A' -> 'JANE A DOE' (the order the other parsers and BC use)."""
    name = re.sub(r"\(CONTINUED\)", "", raw, flags=re.I).strip()
    if "," in name:
        last, first = (p.strip() for p in name.split(",", 1))
        name = f"{first} {last}".strip()
    return " ".join(name.split())


def parse_usbank(pages: list[list[str]]) -> Statement:
    closing, holders, txns, warnings = None, {}, [], []
    kind = None
    current = None      # dict(name, limit, billing, rows) for the block being read

    for page_no, lines in enumerate(pages, start=1):
        for raw in lines:
            line = " ".join(raw.split())
            if not line:
                continue
            up = line.upper()

            if closing is None:
                if m := _U_PERIOD.search(line):
                    closing = _us_date(m["close"])
                elif m := _U_CLOSING.search(line):
                    closing = _us_date(m["close"])

            if m := _U_HOLDER.match(line):
                billing = "BILLING ACCOUNT" in up
                name = "BILLING ACCOUNT" if billing else _us_name(m["name"])
                if current is None or current["name"] != name:     # same name again = continued block
                    current = dict(name=name, billing=billing, rows=[],
                                   limit=money(m["limit"]) if m["limit"] else None)
                kind = None
                continue
            if current is None:
                continue
            if up.startswith("PURCHASES AND OTHER DEBITS"):
                kind = "Purchase"; continue
            if up.startswith("PAYMENTS AND OTHER CREDITS"):
                kind = "Payment" if current["billing"] else "Credit"; continue

            if m := _U_TOTAL.match(line):
                last4 = m["last4"]
                rows = current["rows"]
                for row in rows:
                    row["last4"] = last4
                txns.extend(rows)
                printed = abs(money(m["amt"])) * (-1 if m["cr"] else 1)
                credits = sum(r["amount"] for r in rows if r["type"] != "Purchase")
                holders[last4] = dict(
                    name=current["name"], last4=last4,
                    role="Administrator" if current["billing"] else "Employee",
                    limit=current["limit"],
                    # the printed total is net of credits; reconcile() compares purchases only
                    statement_total=round(printed - credits, 2) if not current["billing"] else printed)
                current = kind = None
                continue

            if kind and (m := _U_TXN.match(line)):
                sign = -1 if (m["cr"] or kind != "Purchase") else 1
                current["rows"].append(dict(
                    name=current["name"], last4="", type=kind,
                    post_date=to_date(m["post"], closing), tran_date=to_date(m["tran"], closing),
                    description=m["desc"].strip(), reference=m["ref"], amount=sign * abs(money(m["amt"])),
                    mcc="", merchant_zip="", details="", page=page_no))

    if current is not None and current["rows"]:
        warnings.append(f"{current['name']}: no 'Total for Account' line found; card number unknown")
        for row in current["rows"]:
            row["last4"] = "0000"
        txns.extend(current["rows"])
    return _finish("usbank", closing, holders, txns, warnings)


def _finish(merchant, closing, holders, txns, warnings) -> Statement:
    tx = pd.DataFrame(txns, columns=TX_COLUMNS)
    ch = pd.DataFrame(list(holders.values()), columns=CH_COLUMNS)
    for last4, grp in tx.groupby("last4"):
        if last4 not in set(ch["last4"]):
            ch.loc[len(ch)] = [grp["name"].iloc[0], last4, "Employee", None, None]
            warnings.append(f"{grp['name'].iloc[0]} (…{last4}) not found in cardholder summary")
    if ch.empty:
        warnings.append("No cardholders found - wrong merchant selected, or not a statement?")
    return Statement(merchant, closing, ch, tx, warnings=warnings)


# ============================================================ registry
MERCHANTS: dict[str, Merchant] = {
    m.id: m for m in [
        Merchant("huntington", "Huntington Business Card", parse_huntington, huntington_looks_like),
        Merchant("usbank", "U.S. Bank Business Card", parse_usbank, usbank_looks_like),
        # Merchant("amex", "American Express Business", parse_amex, amex_looks_like),
    ]
}


# ============================================================ entry points
def lines_from_text_layer(pdf_bytes: bytes) -> list[list[str]]:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [(p.extract_text() or "").splitlines() for p in pdf.pages]


AUTO = "auto"   # BC's default: detect the issuer from the PDF


def detect(pages: list[list[str]]) -> Merchant | None:
    return next((m for m in MERCHANTS.values() if m.looks_like(pages)), None)


def parse_statement(pdf_bytes: bytes, merchant_id: str, method: str = "auto") -> Statement:
    """merchant_id: a MERCHANTS id, or 'auto' to detect it.
    method: 'auto' (text layer, fall back to docTR), 'text', or 'ocr'."""
    if merchant_id == AUTO:
        return _parse_detected(pdf_bytes, method)
    merchant = MERCHANTS.get(merchant_id)
    if merchant is None:
        raise ValueError(f"Unknown merchant '{merchant_id}'. Known: {', '.join(MERCHANTS)}")

    if method in ("auto", "text"):
        pages = lines_from_text_layer(pdf_bytes)
        if method == "text" or merchant.looks_like(pages):
            st = merchant.parse(pages)
            st.method = "PDF text layer"
            return st
        if any(l.strip() for p in pages for l in p):
            # there IS text, it just isn't this issuer's layout -> almost certainly the wrong merchant
            other = [m.label for m in MERCHANTS.values() if m.id != merchant_id and m.looks_like(pages)]
            hint = f" It looks like: {', '.join(other)}." if other else ""
            raise ValueError(f"This PDF doesn't look like a {merchant.label} statement.{hint}")

    from core.ocr import lines_from_pdf

    st = merchant.parse(lines_from_pdf(pdf_bytes))
    st.method = "docTR OCR"
    return st


def _parse_detected(pdf_bytes: bytes, method: str) -> Statement:
    if method in ("auto", "text"):
        pages = lines_from_text_layer(pdf_bytes)
        if merchant := detect(pages):
            st = merchant.parse(pages)
            st.method = "PDF text layer"
            return st
        if method == "text":
            raise ValueError(f"Couldn't recognize the card issuer. This server reads: "
                             f"{', '.join(m.label for m in MERCHANTS.values())}.")

    from core.ocr import lines_from_pdf

    pages = lines_from_pdf(pdf_bytes)
    merchant = detect(pages)
    if merchant is None:
        raise ValueError(f"Couldn't recognize the card issuer. This server reads: "
                         f"{', '.join(m.label for m in MERCHANTS.values())}.")
    st = merchant.parse(pages)
    st.method = "docTR OCR"
    return st


def reconcile(st: Statement) -> pd.DataFrame:
    """Parsed purchase totals vs the per-cardholder total printed on the statement."""
    purch = st.transactions[st.transactions["type"] == "Purchase"].groupby("last4")["amount"].sum()
    df = st.cardholders.copy()
    df["parsed_purchases"] = df["last4"].map(purch).fillna(0.0).round(2)
    emp = df["role"] == "Employee"
    df["matches"] = None
    df.loc[emp, "matches"] = (df.loc[emp, "parsed_purchases"] - df.loc[emp, "statement_total"]).abs() < 0.005
    return df
