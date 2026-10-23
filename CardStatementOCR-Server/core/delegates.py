"""
Delegates: people who may open another person's portal links and approve for them.
Set in config/ocr.env (one pair per entry, entries separated by ';'):

    OCR_DELEGATES=jdoe@example.com=jsmith@example.com=J. Smith

  delegate email = person they act for = that person's name (as printed on the Requisition Form:
  "digitally signed by <delegate> on behalf of <name> at <time>").
"""
from __future__ import annotations

import os


def _pairs() -> list[tuple[str, str, str]]:
    out = []
    for entry in os.environ.get("OCR_DELEGATES", "").split(";"):
        bits = [b.strip() for b in entry.split("=")]
        if len(bits) >= 2 and bits[0] and bits[1]:
            out.append((bits[0].lower(), bits[1].lower(), bits[2] if len(bits) > 2 and bits[2] else bits[1]))
    return out


def principals_for(email: str) -> list[str]:
    """Emails this person may act for."""
    e = (email or "").lower()
    return [p for d, p, _ in _pairs() if d == e]


def delegates_of(email: str) -> list[str]:
    """Emails that may act for this person (CC'd on their approval emails)."""
    e = (email or "").lower()
    return [d for d, p, _ in _pairs() if p == e]


def acting_for(user: str, owner: str) -> str | None:
    """The owner's name when `user` is acting for `owner` as their delegate, else None."""
    u, o = (user or "").lower(), (owner or "").lower()
    if not u or not o or u == o:
        return None
    return next((name for d, p, name in _pairs() if d == u and p == o), None)


def may_open(user: str, owner: str) -> bool:
    u = (user or "").lower()
    return u == (owner or "").lower() or acting_for(u, owner) is not None
