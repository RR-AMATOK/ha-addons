"""Pure recurrence engine for scheduled money (paychecks, bills, standing transfers).

Same shape as `goals.py` / `ventures.py` / `affordability.py`: no I/O, no clock reads inside
the functions, integer cents, dates only. Every "today" is passed in by the caller so the
whole module is deterministic and testable.

Ported from the reference implementation in `docs/mockups/scheduled-money-app.html`, which was
built first and validated against the same known-answer cases now in `tests/test_schedules.py`.

Two things carry most of the design weight:

**Future occurrences are never stored.** A rule is expanded across a bounded window on demand,
so an open-ended schedule costs nothing and can never pollute transaction history. This is what
keeps DEC-009 #3 ("never auto-generate actuals") intact for everything that has not happened yet.

**A limit counts from the rule's own start, not from the view window.** `end='after', end_n=3`
means the third occurrence ever, so asking for a window that begins later still returns the
right ones. Getting this backwards makes a schedule silently immortal.

One convention to carry across the wire: **`month_of_year` is 1-based** (January = 1), matching
SQL, ISO and every other date in this codebase. JavaScript's own `Date` is 0-based, so the
client must convert when it builds a yearly rule. A differential run of 710 rule combinations
against the reference engine agrees exactly once that conversion is applied, and disagrees on
every yearly rule when it is not — which is precisely how this note came to be written.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

# `day_1`/`day_2` sentinel meaning "whatever the last day of this month is". 32 is deliberately
# outside the 1..31 range a real day-of-month can occupy, so it can never collide with one.
LAST_DAY = 32

FREQS = ("daily", "weekly", "semimonthly", "monthly", "yearly")
SHIFTS = ("none", "before", "after")

# Sunday-based, matching the reference implementation and the DB's stored codes. Python's own
# date.weekday() is Monday-based, so every conversion goes through `_dow` rather than being
# open-coded — an off-by-one here moves every weekly schedule by a day.
WEEKDAY_CODES = ("SU", "MO", "TU", "WE", "TH", "FR", "SA")


class ScheduleRuleError(ValueError):
    """A rule that cannot be expanded. Raised at the edge, never swallowed into an empty list —
    a schedule that silently produces nothing is indistinguishable from one that is merely not
    due yet, and that ambiguity is how a missing paycheck goes unnoticed."""


# ---------- small date helpers ----------

def _dow(d: date) -> int:
    """Day of week, Sunday=0 (the convention used by `weekdays` and `WEEKDAY_CODES`)."""
    return (d.weekday() + 1) % 7


def _last_day_of(year: int, month: int) -> int:
    return monthrange(year, month)[1]


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    """Month arithmetic on a (year, month) pair, 1-based month."""
    total = (year * 12) + (month - 1) + n
    return total // 12, (total % 12) + 1


def _mk(year: int, month: int, day: int) -> date:
    """Build a date, resolving the LAST_DAY sentinel and clamping a too-large day to the
    month's own length: the 31st of February is the 28th (or the 29th), never March 3rd."""
    last = _last_day_of(year, month)
    return date(year, month, last if day == LAST_DAY else min(day, last))


def _shift(d: date, mode: str) -> date:
    """Move a weekend date to the adjacent business day.

    Holidays are deliberately out of scope: there is no holiday calendar anywhere in this app,
    and a guessed one would fire wrong every year it guessed badly.
    """
    if mode == "before":
        if _dow(d) == 6:            # Saturday
            return d - timedelta(days=1)
        if _dow(d) == 0:            # Sunday
            return d - timedelta(days=2)
    elif mode == "after":
        if _dow(d) == 6:
            return d + timedelta(days=2)
        if _dow(d) == 0:
            return d + timedelta(days=1)
    return d


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def parse_weekdays(value) -> list[int]:
    """Accept the DB's `'FR'` / `'MO,WE'` string, a list of ints, or None."""
    if value is None or value == "":
        return []
    parts = value.split(",") if isinstance(value, str) else list(value)
    out = set()
    for part in parts:
        # Accept both forms in either container: the DB stores 'FR' / 'MO,WE', while callers
        # and tests are happier passing ints. A list of codes reached the int() branch in the
        # first draft and blew up on `int('FR')`.
        if isinstance(part, str):
            code = part.strip().upper()
            if not code:
                continue
            if code.isdigit():
                out.add(int(code))
                continue
            if code not in WEEKDAY_CODES:
                raise ScheduleRuleError(f"unknown weekday code {part!r}")
            out.add(WEEKDAY_CODES.index(code))
        else:
            out.add(int(part))
    for wd in out:
        if not 0 <= wd <= 6:
            raise ScheduleRuleError(f"weekday must be 0..6 (Sunday=0), got {wd}")
    return sorted(out)


# ---------- the rule ----------

class Rule:
    """A normalized recurrence rule. Built from either the DB row's columns or a plain dict;
    validation happens once, here, so the expansion loops below can stay simple."""

    __slots__ = ("freq", "interval", "weekdays", "day_1", "day_2", "month_of_year",
                 "anchor_on", "ends_on", "end_mode", "end_count", "weekend_shift")

    def __init__(self, freq, anchor_on, interval=1, weekdays=None, day_1=None, day_2=None,
                 month_of_year=None, ends_on=None, end_mode="never", end_count=None,
                 weekend_shift="none"):
        if freq not in FREQS:
            raise ScheduleRuleError(f"freq must be one of {FREQS}, got {freq!r}")
        if weekend_shift not in SHIFTS:
            raise ScheduleRuleError(f"weekend_shift must be one of {SHIFTS}, got {weekend_shift!r}")
        if end_mode not in ("never", "on", "after"):
            raise ScheduleRuleError(f"end_mode must be never|on|after, got {end_mode!r}")
        # `int(interval or 1)` looks equivalent and is not: 0 is falsy, so an explicit
        # interval of 0 became 1 and an obviously-broken rule silently expanded every period.
        if interval is None:
            interval = 1
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            raise ScheduleRuleError(f"interval must be a whole number, got {interval!r}")
        if interval < 1:
            raise ScheduleRuleError(f"interval must be >= 1, got {interval}")

        self.freq = freq
        self.interval = interval
        self.anchor_on = parse_date(anchor_on)
        self.weekdays = parse_weekdays(weekdays)
        self.day_1 = int(day_1) if day_1 is not None else None
        self.day_2 = int(day_2) if day_2 is not None else None
        self.month_of_year = int(month_of_year) if month_of_year is not None else None
        self.weekend_shift = weekend_shift
        self.end_mode = end_mode
        self.ends_on = parse_date(ends_on) if ends_on else None
        self.end_count = int(end_count) if end_count is not None else None

        for name in ("day_1", "day_2"):
            v = getattr(self, name)
            if v is not None and not (1 <= v <= LAST_DAY):
                raise ScheduleRuleError(f"{name} must be 1..31 or {LAST_DAY} (last day), got {v}")
        if self.month_of_year is not None and not (1 <= self.month_of_year <= 12):
            raise ScheduleRuleError(f"month_of_year must be 1..12, got {self.month_of_year}")
        if self.end_mode == "on" and self.ends_on is None:
            raise ScheduleRuleError("end_mode='on' needs ends_on")
        if self.end_mode == "after" and not (self.end_count and self.end_count > 0):
            raise ScheduleRuleError("end_mode='after' needs a positive end_count")
        if self.freq == "semimonthly" and (self.day_1 is None or self.day_2 is None):
            raise ScheduleRuleError("semimonthly needs both day_1 and day_2")
        if self.freq == "semimonthly" and self.day_1 == self.day_2:
            raise ScheduleRuleError("semimonthly needs two different days")

    @classmethod
    def from_row(cls, row) -> "Rule":
        """Build from a `schedule` table row (sqlite3.Row or dict)."""
        g = row.__getitem__ if hasattr(row, "keys") else row.get
        def col(name, default=None):
            try:
                v = g(name)
            except (KeyError, IndexError):
                return default
            return default if v is None else v
        return cls(
            freq=col("freq"),
            anchor_on=col("anchor_on"),
            interval=col("interval_n", 1),
            weekdays=col("weekdays"),
            day_1=col("day_1"),
            day_2=col("day_2"),
            month_of_year=col("month_of_year"),
            ends_on=col("ends_on"),
            end_mode=col("end_mode", "never"),
            end_count=col("end_count"),
            weekend_shift=col("weekend_shift", "none"),
        )


# ---------- expansion ----------

class _Emitter:
    """Applies the end conditions, the weekend shift and the window filter to each candidate
    date the frequency loops produce, and reports whether generation should continue.

    Kept as one object because the `produced` counter is the subtle part: it counts every
    occurrence the rule has ever produced from its anchor, so an `after N` limit is not
    renumbered by asking for a later window.
    """

    def __init__(self, rule: Rule, window_start: date, window_end: date, cap: int):
        self.rule = rule
        self.start = window_start
        self.end = window_end
        self.cap = cap
        self.produced = 0
        self.out: list[dict] = []

    def emit(self, raw: date) -> bool:
        r = self.rule
        if raw < r.anchor_on:
            return True                       # before the series begins; keep looking
        if r.end_mode == "on" and r.ends_on is not None and raw > r.ends_on:
            return False
        self.produced += 1
        if r.end_mode == "after" and self.produced > (r.end_count or 0):
            return False
        shifted = _shift(raw, r.weekend_shift)
        if self.start <= shifted <= self.end:
            self.out.append({"on": shifted, "raw": raw})
        if len(self.out) >= self.cap:
            return False
        return raw <= self.end


def occurrences(rule, window_start, window_end, cap: int = 400) -> list[dict]:
    """Every occurrence of `rule` landing in [window_start, window_end], inclusive.

    Returns dicts of ``{"on": date, "raw": date}`` sorted by ``on``. ``raw`` is the date the
    rule itself produced and ``on`` is where it actually lands after any weekend shift — the
    pair is what lets the UI say *why* a date moved instead of silently moving it.

    `cap` bounds the result so an open-ended daily rule over a decade cannot allocate without
    limit; the window is the real control.
    """
    if not isinstance(rule, Rule):
        rule = Rule.from_row(rule) if hasattr(rule, "keys") or isinstance(rule, dict) else rule
    if not isinstance(rule, Rule):
        raise ScheduleRuleError("rule must be a Rule, a dict, or a schedule row")

    ws, we = parse_date(window_start), parse_date(window_end)
    if we < ws:
        return []
    em = _Emitter(rule, ws, we, cap)
    anchor = rule.anchor_on

    if rule.freq == "daily":
        d = anchor
        while em.emit(d):
            d += timedelta(days=rule.interval)

    elif rule.freq == "weekly":
        days = rule.weekdays or [_dow(anchor)]
        # Phase is counted in whole weeks from the Sunday of the anchor's week, so
        # "every other Friday" keeps its parity across month and year boundaries.
        anchor_sunday = anchor - timedelta(days=_dow(anchor))
        week = 0
        while True:
            week_start = anchor_sunday + timedelta(days=week * rule.interval * 7)
            if week_start > we and week_start > anchor:
                break
            keep_going = True
            for wd in days:
                if not em.emit(week_start + timedelta(days=wd)):
                    keep_going = False
                    break
            if not keep_going:
                break
            week += 1

    elif rule.freq == "semimonthly":
        i = 0
        while True:
            y, m = _add_months(anchor.year, anchor.month, i)
            if date(y, m, 1) > we and i > 0:
                break
            pair = sorted([_mk(y, m, rule.day_1), _mk(y, m, rule.day_2)])
            keep_going = True
            for d in pair:
                if not em.emit(d):
                    keep_going = False
                    break
            if not keep_going:
                break
            i += 1

    elif rule.freq == "monthly":
        day = rule.day_1 if rule.day_1 is not None else anchor.day
        i = 0
        while True:
            y, m = _add_months(anchor.year, anchor.month, i)
            if date(y, m, 1) > we and i > 0:
                break
            if not em.emit(_mk(y, m, day)):
                break
            i += rule.interval

    elif rule.freq == "yearly":
        month = rule.month_of_year if rule.month_of_year is not None else anchor.month
        day = rule.day_1 if rule.day_1 is not None else anchor.day
        i = 0
        while True:
            y = anchor.year + i
            if date(y, month, 1) > we and i > 0:
                break
            if not em.emit(_mk(y, month, day)):
                break
            i += rule.interval

    em.out.sort(key=lambda o: o["on"])
    return em.out


def next_occurrence(rule, after, horizon_days: int = 800) -> dict | None:
    """The first occurrence strictly on or after `after`, or None if the series is finished.

    `horizon_days` bounds the search; a schedule whose next hit is further out than that is
    reported as None rather than scanned for indefinitely.
    """
    a = parse_date(after)
    hits = occurrences(rule, a, a + timedelta(days=horizon_days), cap=1)
    return hits[0] if hits else None


# ---------- exceptions ----------

def apply_exceptions(hits: list[dict], exceptions) -> list[dict]:
    """Fold per-occurrence overrides into an expansion.

    An exception is keyed by the occurrence's ``raw`` date — the date the *rule* produced —
    because that is the only stable identity an occurrence has. Keying on the shifted date
    would break the link the moment someone changed the weekend-shift setting.

    Supported actions: ``skip`` (drop it) and ``override`` (change the date and/or the amount
    and/or the description for this one occurrence only).
    """
    by_raw: dict[date, dict] = {}
    for ex in exceptions or []:
        g = ex.__getitem__ if hasattr(ex, "keys") else ex.get
        raw = parse_date(g("occurrence_on"))
        by_raw[raw] = ex

    out = []
    for hit in hits:
        ex = by_raw.get(hit["raw"])
        if ex is None:
            out.append(dict(hit))
            continue
        g = ex.__getitem__ if hasattr(ex, "keys") else ex.get
        action = g("action")
        if action == "skip":
            continue
        if action != "override":
            raise ScheduleRuleError(f"unknown exception action {action!r}")
        item = dict(hit)
        moved = None
        try:
            moved = g("moved_to")
        except (KeyError, IndexError):
            moved = None
        if moved:
            item["on"] = parse_date(moved)
        for field, key in (("amount_cents", "amount_cents"), ("description", "description")):
            try:
                v = g(key)
            except (KeyError, IndexError):
                v = None
            if v is not None:
                item[field] = v
        item["overridden"] = True
        out.append(item)
    out.sort(key=lambda o: o["on"])
    return out


def expand(rule, exceptions, window_start, window_end, cap: int = 400) -> list[dict]:
    """`occurrences` + `apply_exceptions` in one call — the form callers actually want."""
    return apply_exceptions(occurrences(rule, window_start, window_end, cap), exceptions)
