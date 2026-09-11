"""Business-day math. This predicate IS the tool, so it lives on its own."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from . import config

NY = ZoneInfo(config.TZ)


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def to_utc(value: str | None) -> str | None:
    """Canonicalise any incoming timestamp to a UTC ISO string.

    Google returns local offsets (2026-08-10T16:30:00-04:00) and all-day events
    return a bare date; Instantly returns Z. These end up compared as STRINGS in
    SQL, where '-04:00' sorts before '+00:00' regardless of the actual instant —
    so a late-afternoon meeting can read as already finished. One form at every
    write makes the whole class impossible rather than patching each comparison.
    """
    if not value:
        return None
    v = value.strip()
    try:
        if len(v) == 10 and v[4] == "-":              # all-day event: bare date
            dt = datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return v                                       # unparseable: leave it alone
    return dt.astimezone(timezone.utc).isoformat()


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def due_at(clock_start: datetime, business_days: int = config.SLA_BUSINESS_DAYS) -> datetime:
    """Walk forward N business days, skipping weekends, preserving time of day.

    Fri 16:00 -> Mon 16:00 (1) -> Tue 16:00 (2).
    A weekend anchor has no valid time of day, so it normalises to Mon 09:00 first.
    """
    local = clock_start.astimezone(NY)
    if local.weekday() >= 5:
        while local.weekday() >= 5:
            local += timedelta(days=1)
        local = local.replace(hour=9, minute=0, second=0, microsecond=0)
    for _ in range(business_days):
        local += timedelta(days=1)
        while local.weekday() >= 5:
            local += timedelta(days=1)
    return local.astimezone(timezone.utc)


def in_quiet_hours(dt: datetime) -> bool:
    """Delivery rule only — never folded into due_at. Nothing after Fri 17:00
    until Mon 09:00."""
    l = dt.astimezone(NY)
    if l.weekday() >= 5:
        return True
    if l.weekday() == 4 and l.hour >= 17:
        return True
    return l.hour < 9 or l.hour >= 18


def next_delivery(dt: datetime) -> datetime:
    """Earliest moment at or after `dt` that isn't quiet hours."""
    l = dt.astimezone(NY)
    while True:
        if l.weekday() >= 5:
            l = (l + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        elif l.weekday() == 4 and l.hour >= 17:
            l = (l + timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
        elif l.hour < 9:
            l = l.replace(hour=9, minute=0, second=0, microsecond=0)
        elif l.hour >= 18:
            l = (l + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        else:
            return l.astimezone(timezone.utc)


def send_target(reply_hours: list[int] | None = None, after: datetime | None = None) -> datetime:
    """When to actually deliver the email: two minutes from now, inside the
    weekday send window.

    The two minutes are the undo window — Cancel on the card is a real undo only
    because the send is not instant. The window clamp is the one piece of the
    old scheduler the data supported (69% of replies land 08:00-18:00). The
    per-lead reply hour it used to pick is gone: it applied to 15% of leads and
    was read in UTC then applied on a New York clock, so half of those were
    scheduled hours late. `reply_hours` is accepted and ignored so callers did
    not have to change.
    """
    after = after or datetime.now(timezone.utc)
    lo, hi = config.SEND_WINDOW
    l = (after + timedelta(minutes=2)).astimezone(NY)
    if l.weekday() < 5 and lo <= l.hour < hi:
        target = l
    else:
        target = l.replace(hour=lo, minute=0, second=0, microsecond=0)
        if l.weekday() >= 5 or l.hour >= hi:
            target += timedelta(days=1)
        while target.weekday() >= 5:
            target += timedelta(days=1)
    return target.astimezone(timezone.utc)
