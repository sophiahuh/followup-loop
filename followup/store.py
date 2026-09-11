"""SQLite store. The poller is the only writer; thread state is *recomputed*
from the emails table rather than mutated, so replay and restarts are safe."""
import sqlite3, json
from datetime import datetime, timezone
from . import config, clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails(
  id TEXT PRIMARY KEY, thread_id TEXT, campaign_id TEXT, lead TEXT,
  from_email TEXT, to_email TEXT, eaccount TEXT, ue_type INTEGER, subject TEXT,
  timestamp_created TEXT, timestamp_email TEXT, is_outbound INTEGER, raw TEXT);
CREATE INDEX IF NOT EXISTS ix_emails_thread ON emails(thread_id, timestamp_email);
CREATE INDEX IF NOT EXISTS ix_emails_lead   ON emails(lead);

CREATE TABLE IF NOT EXISTS threads(
  thread_id TEXT PRIMARY KEY, lead TEXT, campaign_id TEXT,
  state TEXT NOT NULL DEFAULT 'ACTIVE',
  last_inbound_at TEXT, last_outbound_at TEXT, clock_start_at TEXT, due_at TEXT,
  awaiting_us INTEGER DEFAULT 0, msg_count INTEGER DEFAULT 0,
  interest_status INTEGER, hold_reason TEXT,
  first_seen_at TEXT, backfilled INTEGER DEFAULT 0, notified_at TEXT);
CREATE INDEX IF NOT EXISTS ix_threads_due ON threads(state, due_at);

CREATE TABLE IF NOT EXISTS campaigns(
  campaign_id TEXT PRIMARY KEY, name TEXT, status INTEGER,
  tracked INTEGER, resolved_at TEXT);

CREATE TABLE IF NOT EXISTS drafts(
  thread_id TEXT, strategy TEXT, body TEXT, recommended INTEGER DEFAULT 0,
  generated_at TEXT, PRIMARY KEY(thread_id, strategy));

CREATE TABLE IF NOT EXISTS cards(
  thread_id TEXT PRIMARY KEY, channel TEXT, ts TEXT, strategy TEXT,
  posted_at TEXT, scheduled_for TEXT, sent_at TEXT, sent_email_id TEXT,
  body TEXT, cancelled INTEGER DEFAULT 0, actor TEXT);

CREATE TABLE IF NOT EXISTS meetings(
  event_id TEXT PRIMARY KEY, calendar_id TEXT, thread_id TEXT, lead TEXT,
  matched_address TEXT, match_type TEXT, summary TEXT,
  start_at TEXT, end_at TEXT, status TEXT, response_status TEXT,
  disposition TEXT, dispositioned_at TEXT, updated_at TEXT, event_updated TEXT);
CREATE INDEX IF NOT EXISTS ix_meetings_thread ON meetings(thread_id, start_at);

CREATE TABLE IF NOT EXISTS examples(
  email_id TEXT PRIMARY KEY, thread_id TEXT, lead TEXT, motion TEXT, body TEXT,
  sent_at TEXT, got_reply INTEGER DEFAULT 0, source TEXT DEFAULT 'history');
CREATE INDEX IF NOT EXISTS ix_examples_motion ON examples(motion, source, sent_at);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

LIVE_STATES = ("ACTIVE",)

# A thread's relationship to any meeting. Derived, never mutated in place — the
# email poller owns `emails`, the calendar sync owns `meetings`, and thread state
# is recomputed from both. That keeps a single derivation path with two feeds
# rather than two writers racing on one row.
MEET_NONE, MEET_UPCOMING, MEET_JUST_HAPPENED = "none", "upcoming", "just_happened"
MEET_HAD_CALL, MEET_CANCELLED, MEET_DECLINED = "had_call", "cancelled", "declined"
# Google only lets the ORGANIZER cancel an event; a guest can only decline. So
# status=cancelled on an event we organised means WE cancelled it. Two flavours:
# an invite the lead never accepted that we withdrew (they never agreed to a
# meeting - not a cancellation from their point of view), and one they had
# accepted that we then cancelled (we owe them the reschedule). A booking-tool
# event has the lead as organizer, so cancelled there stays "their" cancellation.
MEET_INVITE_WITHDRAWN, MEET_WE_CANCELLED = "invite_withdrawn", "we_cancelled"


def _cancel_kind(m):
    # A decline is the lead's act and outranks whatever we did to the event
    # afterwards - deleting a declined invite is housekeeping, not a withdrawal.
    if (m["response_status"] or "") == "declined":
        return MEET_DECLINED
    # "We cancelled" is only certain for a HAND-SENT invite: on a Google event the
    # organizer alone can cancel, and a human on our side organised it. A booking
    # made through our HubSpot link ALSO shows Daniel as organizer, but the lead
    # booked it and can cancel it from the confirmation email - so the side is
    # unknown and it stays a plain cancellation (assumed theirs, per Sophia).
    org = (m["organizer"] or "") if "organizer" in m.keys() else ""
    tool = (m["booking_tool"] or "") if "booking_tool" in m.keys() else ""
    if "crustdata" in org.lower() and tool == "manual":
        return MEET_WE_CANCELLED if (m["response_status"] or "") == "accepted" else MEET_INVITE_WITHDRAWN
    return MEET_CANCELLED


MIGRATIONS = [("threads", "hold_reason", "TEXT"), ("cards", "resolved_at", "TEXT"),
              ("threads", "meeting_state", "TEXT"),
              ("meetings", "event_updated", "TEXT"),
              ("cards", "override_at", "TEXT"),
              ("cards", "actor", "TEXT"),
              ("threads", "human_reason", "TEXT"),
              ("cards", "send_error", "TEXT"),
              ("meetings", "organizer", "TEXT"),
              ("meetings", "booking_tool", "TEXT"),
              ("threads", "enriched_emails", "TEXT"),    # JSON list of work aliases from Crustdata
              ("threads", "enriched_at", "TEXT"),
              ("threads", "enrich_note", "TEXT"),        # "Michael Rossiter @ Atomic" for the card
              ("cards", "draft_body", "TEXT")]           # the draft as shown at click time - edit rate


def connect(path=None):
    conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:          # additive only; safe to re-run
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    return conn


def get_meta(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def is_ours(from_email: str | None, eaccount: str | None) -> bool:
    """Ours if it came from the sending account, or from any internal domain —
    the latter catches teammates who join a thread from their own address."""
    if not from_email:
        return False
    f = from_email.lower()
    if eaccount and f == eaccount.lower():
        return True
    return config.INTERNAL_DOMAIN_TOKEN in f.split("@")[-1]


def upsert_email(conn, e: dict) -> bool:
    """Idempotent by email id. Returns True if this row was new."""
    eaccount = e.get("eaccount")
    frm = e.get("from_address_email")
    to = e.get("to_address_email_list")
    row = (e["id"], e.get("thread_id"), e.get("campaign_id"), e.get("lead"),
           frm, to if isinstance(to, str) else json.dumps(to), eaccount,
           e.get("ue_type"), e.get("subject"),
           e.get("timestamp_created"), e.get("timestamp_email") or e.get("timestamp_created"),
           1 if is_ours(frm, eaccount) else 0,
           json.dumps(e))
    before = conn.total_changes
    conn.execute("""INSERT INTO emails(id,thread_id,campaign_id,lead,from_email,to_email,
        eaccount,ue_type,subject,timestamp_created,timestamp_email,is_outbound,raw)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""", row)
    return conn.total_changes > before


def recompute_thread(conn, thread_id: str):
    """Derive thread state from the observations, never from incremental mutation."""
    rows = conn.execute(
        "SELECT * FROM emails WHERE thread_id=? ORDER BY timestamp_email", (thread_id,)).fetchall()
    if not rows:
        return
    lead = next((r["lead"] for r in rows if r["lead"]), None)
    campaign_id = rows[-1]["campaign_id"]
    outs = [r["timestamp_email"] for r in rows if r["is_outbound"]]
    ins = [r["timestamp_email"] for r in rows if not r["is_outbound"]]
    last_out, last_in = (max(outs) if outs else None), (max(ins) if ins else None)

    # The clock anchors on the most recent message in EITHER direction. A lead
    # who replied today is not a follow-up candidate today — Sophia answers
    # those herself in Instantly. They become one only if two business days
    # pass with nothing from us. (Anchoring on our last outbound alone carded
    # Mya one minute after she replied, because our email was already stale.)
    anchor = max(x for x in (last_out, last_in) if x) if (last_out or last_in) else None
    due = clock.iso(clock.due_at(clock.parse(anchor))) if anchor else None
    mstate_pre, mrow = meeting_state(conn, thread_id)
    if mstate_pre in (MEET_CANCELLED, MEET_DECLINED, MEET_WE_CANCELLED) and mrow is not None:
        # A cancellation is a warm, same-day moment — waiting two business days
        # wastes the intent. Due as soon as we learn about it, unless we have
        # already written since.
        seen = mrow["event_updated"] or mrow["updated_at"]
        if seen and (not last_out or last_out < seen):
            anchor, due = seen, seen
    awaiting = 1 if (last_in and (not last_out or last_in > last_out)) else 0

    existing = conn.execute("SELECT state, first_seen_at FROM threads WHERE thread_id=?",
                            (thread_id,)).fetchone()
    state = existing["state"] if existing else "ACTIVE"
    mstate = mstate_pre
    if mstate in (MEET_JUST_HAPPENED, MEET_HAD_CALL):
        state = "CLOSED_CALL"              # a call happened: the loop is over
    elif state == "CLOSED_CALL" and mstate in (MEET_CANCELLED, MEET_DECLINED, MEET_WE_CANCELLED,
                                                MEET_INVITE_WITHDRAWN):
        state = "ACTIVE"                   # cancelled, declined or no-showed: back
    first_seen = existing["first_seen_at"] if existing else datetime.now(timezone.utc).isoformat()

    conn.execute("""INSERT INTO threads(thread_id,lead,campaign_id,state,last_inbound_at,
        last_outbound_at,clock_start_at,due_at,awaiting_us,msg_count,first_seen_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(thread_id) DO UPDATE SET lead=excluded.lead,
          campaign_id=excluded.campaign_id, last_inbound_at=excluded.last_inbound_at,
          last_outbound_at=excluded.last_outbound_at, clock_start_at=excluded.clock_start_at,
          due_at=excluded.due_at, awaiting_us=excluded.awaiting_us,
          msg_count=excluded.msg_count""",
        (thread_id, lead, campaign_id, state, last_in, last_out, anchor, due,
         awaiting, len(rows), first_seen))
    # `state` is deliberately absent from the upsert's DO UPDATE so recompute can
    # never clobber a manual STOPPED / NOT_INTERESTED. Meeting-driven transitions
    # are therefore applied here, and only between ACTIVE and CLOSED_CALL.
    conn.execute("""UPDATE threads SET meeting_state=?,
        state = CASE
          WHEN state NOT IN ('ACTIVE','CLOSED_CALL') THEN state
          WHEN ? IN ('just_happened','had_call')     THEN 'CLOSED_CALL'
          ELSE 'ACTIVE' END
        WHERE thread_id=?""", (mstate, mstate, thread_id))


def send_block_reason(conn, thread_id: str, override: bool = False) -> str | None:
    """Every reason a follow-up must not go out, in one place.

    Called at click time AND immediately before the network call, because the
    scheduled send can sit for 20-70 hours and every guard the tool has can
    change during it. An override waives ONLY an upcoming meeting — a call that
    has actually happened is never waivable, whatever was agreed beforehand.
    """
    from . import config
    t = conn.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
    if t is None:
        return "the thread no longer exists"
    if t["state"] != "ACTIVE":
        return "the thread is " + str(t["state"]).lower().replace("_", " ")
    domain = (t["lead"] or "").split("@")[-1].lower()
    if domain in config.CUSTOMER_DOMAINS:
        return domain + " is an existing customer"

    state, m = meeting_state(conn, thread_id)
    when = (m["start_at"] or "")[:10] if m is not None else ""
    if state in (MEET_JUST_HAPPENED, MEET_HAD_CALL):
        return "they were on a call on " + when          # never waivable
    if state == MEET_UPCOMING and not override:
        return "they have a meeting booked for " + when

    card = conn.execute("SELECT posted_at FROM cards WHERE thread_id=?", (thread_id,)).fetchone()
    row = newer_outbound(conn, thread_id, card["posted_at"] if card else None)
    if row is not None:
        return "you already replied in Instantly at " + str(row["timestamp_email"])[:16]
    return None


def newer_outbound(conn, thread_id: str, since: str | None):
    """Any outbound on this thread after `since`. The guard against sending twice
    when a reply went out through Instantly and the poller hasn't caught up yet."""
    if not since:
        return None
    return conn.execute("""SELECT timestamp_email FROM emails WHERE thread_id=?
        AND is_outbound=1 AND timestamp_email > ? ORDER BY timestamp_email DESC LIMIT 1""",
        (thread_id, since)).fetchone()


def meeting_state(conn, thread_id: str):
    """(state, meeting_row) for the most recent meeting on this thread.

    A started, uncancelled meeting closes the thread permanently — getting people
    onto calls is the whole job, and what happens afterwards is handled outside
    this tool. Only a cancellation, a decline, or a no-show returns it.
    """
    # Scoped to the LEAD, not the thread. gcal.sync attaches a meeting to exactly
    # one thread (the first that claimed the address), so a lead with two threads
    # had a call recorded on one and no meeting rows at all on the other — and the
    # sibling sailed through every guard. Measured live on ji@openwellhealth.com.
    lead = conn.execute("SELECT lead FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
    lead = lead["lead"] if lead else None
    rows = conn.execute("""SELECT * FROM meetings
        WHERE thread_id=? OR (? IS NOT NULL AND lead=?)
        ORDER BY start_at DESC""", (thread_id, lead, lead)).fetchall()
    if not rows:
        return MEET_NONE, None

    # A completed call is TERMINAL and outranks everything dated after it. Without
    # this, a meeting cancelled later — an internal kick-off, a rescheduled sync —
    # reads as the thread's current state and reopens someone who has already been
    # on a call, which is the one outcome this system must never produce.
    had = next((r for r in rows if r["disposition"] == "had_call"), None)
    if had is not None:
        return MEET_HAD_CALL, had

    # A meeting that already started and was not cancelled, declined or no-showed
    # is terminal WHETHER OR NOT anyone has dispositioned it. Previously only an
    # explicit had_call rescued a thread, so an un-answered call was erased by any
    # later cancellation and the lead was prospected again.
    now = datetime.now(timezone.utc).isoformat()
    happened = next((r for r in rows
                     if (r["start_at"] or "") <= now
                     and r["status"] == "confirmed"
                     and (r["response_status"] or "") != "declined"
                     and (r["disposition"] or "") != "no_show"), None)
    if happened is not None:
        return MEET_JUST_HAPPENED, happened

    m = rows[0]
    if m["disposition"] == "no_show":
        return _cancel_kind(m), m          # no call happened: back in the loop
    if m["status"] == "cancelled":
        return _cancel_kind(m), m
    if m["response_status"] == "declined":
        return MEET_DECLINED, m
    if (m["start_at"] or "") > now:
        return MEET_UPCOMING, m
    if not m["disposition"]:
        return MEET_JUST_HAPPENED, m
    return MEET_HAD_CALL, m


def tracked_ids(conn) -> set:
    return {r["campaign_id"] for r in
            conn.execute("SELECT campaign_id FROM campaigns WHERE tracked=1")}


def due_threads(conn, now_iso: str, horizon_iso: str | None = None):
    """Live threads past their deadline. awaiting_us floats to the top —
    a lead who replied and got silence is more urgent than one who never wrote."""
    customers = sorted(config.CUSTOMER_DOMAINS)
    # placeholders are built from the config at call time — hard-coding the count
    # means the query silently breaks the next time a domain is added
    q = f"""SELECT * FROM threads WHERE state IN ({','.join('?' * len(LIVE_STATES))})
            AND due_at IS NOT NULL AND due_at <= ?
            -- Re-arms rather than latching. notified_at IS NULL was a one-shot:
            -- once a card was posted the thread could never be selected again, so
            -- a follow-up that got no reply was never chased a second time. When
            -- a send lands, due_at moves past notified_at and the thread returns.
            -- An untouched card does not re-post, because due_at has not moved.
            AND (notified_at IS NULL OR notified_at < due_at)
            AND interest_status = ?
            AND (? IS NULL OR due_at >= ?)
            AND COALESCE(meeting_state,'none') NOT IN ('upcoming','just_happened','had_call')
            AND SUBSTR(lead, INSTR(lead,'@') + 1) NOT IN ({','.join('?' * len(customers))})
            ORDER BY awaiting_us DESC, due_at ASC"""
    return conn.execute(q, (*LIVE_STATES, now_iso, config.INTERESTED,
                            horizon_iso, horizon_iso, *customers)).fetchall()
