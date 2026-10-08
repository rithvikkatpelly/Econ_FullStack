"""
Date phrases the agents' planners share: named economic events and
"before YYYY" windows, so "since the pandemic" means March 2020 everywhere
instead of falling through to a default window.

Every reading here is an assumption about what the user meant, so each comes
with a note the planner surfaces (QueryPlan.assumptions) rather than a silent
guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# (pattern, start date, label). Starts follow the usual reference points:
# NBER business-cycle peaks for recessions, the Fed's first hike for a
# tightening cycle.
_EVENTS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(pandemic|covid(-?19)?|coronavirus|lockdowns?)\b"),
     "2020-03-01", "the pandemic (March 2020)"),
    (re.compile(r"\b(great recession|financial crisis|2008 crisis|housing crash|subprime)\b"),
     "2007-12-01", "the Great Recession (NBER peak, December 2007)"),
    (re.compile(r"\b(dot-?com (bust|crash|bubble))\b"),
     "2001-03-01", "the dot-com bust (NBER peak, March 2001)"),
    (re.compile(r"\b(rate hikes?|hiking cycle|tightening cycle"
                r"|fed (started|began) (raising|hiking))\b"),
     "2022-03-01", "the Fed's 2022 hiking cycle (first hike, March 2022)"),
]

_BEFORE_YEAR_RE = re.compile(r"\b(?:pre-?|before\s+)((?:19|20)\d{2})\b")
_NOW_VS_YEAR_RE = re.compile(
    r"\b(now|today|currently|current(ly)?|these days)\b.*\b(compare[sd]?|vs\.?|versus|than)\b"
    r".*\b((?:19|20)\d{2})\b"
    r"|\b(compare[sd]?)\b.*\b((?:19|20)\d{2})\b.*\b(now|today)\b"
)

BEFORE_YEARS = 5  # how far back a "before YYYY" window reaches


@dataclass(frozen=True)
class Window:
    start: str  # YYYY-MM-DD
    end: str    # YYYY-MM-DD
    note: str   # the assumption, for the plan


def event_window(text: str, today: date | None = None) -> Window | None:
    """A named event ("since the pandemic") → from its start to today."""
    today = today or date.today()
    t = text.lower()
    for pattern, start, label in _EVENTS:
        if pattern.search(t):
            return Window(start, today.isoformat(),
                          f"read '{pattern.search(t).group(0)}' as since {label}")
    return None


_THIS_YEAR_RE = re.compile(r"\b(this year|so far this year|year[- ]to[- ]date|ytd)\b")


def this_year_window(text: str, today: date | None = None) -> Window | None:
    """'this year' / 'year to date' → January 1 to today."""
    today = today or date.today()
    if not _THIS_YEAR_RE.search(text.lower()):
        return None
    return Window(f"{today.year}-01-01", today.isoformat(), "")


def before_year_window(text: str) -> Window | None:
    """'pre-2008' / 'before 2008' → the BEFORE_YEARS years up to it."""
    m = _BEFORE_YEAR_RE.search(text.lower())
    if not m:
        return None
    year = int(m.group(1))
    start, end = f"{year - BEFORE_YEARS}-01-01", f"{year - 1}-12-01"
    return Window(start, end, f"read '{m.group(0)}' as {start}..{end} "
                              f"(the {BEFORE_YEARS} years before {year})")


def now_vs_year(text: str) -> int | None:
    """The year in a two-points-in-time comparison ("now compared to 2008"),
    or None. The window then has to span both points, and the plan should
    say the question is about the two ends, not the path between."""
    m = _NOW_VS_YEAR_RE.search(text.lower())
    if not m:
        return None
    return int(m.group(4) or m.group(6))
