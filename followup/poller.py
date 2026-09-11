"""The email poller. One unfiltered request per cycle regardless of campaign count."""
import json, re
from datetime import datetime, timedelta, timezone
from . import config, store, clock
from .instantly import Instantly

DRAFT_STATUS = 0


def normalise(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def matches(name: str) -> bool:
    return config.MATCH_PHRASE in normalise(name)


def resolve_campaigns(inst: Instantly, conn) -> dict:
    """Hourly. Resolves the name rule to concrete ids so the hot path only ever
    sees UUIDs. Draft campaigns are excluded (they hold no emails anyway), and a
    campaign with live threads is never evicted by a rename."""
    seen, near_miss, tracked = 0, [], 0
    live_threads = {r["campaign_id"] for r in conn.execute(
        "SELECT DISTINCT campaign_id FROM threads WHERE state='ACTIVE'") if r["campaign_id"]}
    now = datetime.now(timezone.utc).isoformat()
    for c in inst.campaigns():
        seen += 1
        n = normalise(c["name"])
        is_match = config.MATCH_PHRASE in n
        if "glove" in n and not is_match:
            near_miss.append(c["name"])
        keep = (is_match and c.get("status") != DRAFT_STATUS) or c["id"] in live_threads
        tracked += 1 if keep else 0
        conn.execute("""INSERT INTO campaigns(campaign_id,name,status,tracked,resolved_at)
            VALUES(?,?,?,?,?) ON CONFLICT(campaign_id) DO UPDATE SET name=excluded.name,
            status=excluded.status, tracked=excluded.tracked, resolved_at=excluded.resolved_at""",
            (c["id"], c["name"], c.get("status"), 1 if keep else 0, now))
    conn.commit()
    return {"seen": seen, "tracked": tracked, "near_miss": near_miss}


def sync_interest(inst: Instantly, conn, days: int = 90) -> dict:
    """Define the universe: leads Instantly has flagged Interested.

    The status lives on emails, not on the lead object (POST /leads/list ignores
    its documented filter values). We only need the lead -> latest-status mapping
    here, so we collect that and skip writing email rows — thread content arrives
    via backfill, which is targeted. Bounded to `days` so this stays a few pages.
    """
    tracked = store.tracked_ids(conn)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")

    # Interest is a THIRD external feed alongside emails and meetings, and it needs
    # an owner. It cannot be derived from the emails the poller already ingests:
    # i_status is denormalised onto every email in both directions, and
    # upsert_email is ON CONFLICT DO NOTHING, so a thread's stored status freezes
    # at first fetch and can never express a downgrade.
    latest = {}
    for st in (config.INTERESTED,) + tuple(config.EXCLUDED_STATUSES):
        for e in inst.emails(i_status=st, sort_order="desc", min_timestamp_created=since):
            lead, ts = e.get("lead"), e.get("timestamp_email") or ""
            if not lead or e.get("campaign_id") not in tracked or not e.get("thread_id"):
                continue
            cur = latest.get(lead)
            if cur is None or ts > cur[1]:
                latest[lead] = (st, ts, set())
            if latest[lead][0] == st:
                latest[lead][2].add(e["thread_id"])

    now = datetime.now(timezone.utc).isoformat()
    for lead, (st, _ts, tids) in latest.items():
        for tid in tids:
            conn.execute("""INSERT INTO threads(thread_id,lead,interest_status,first_seen_at)
                VALUES(?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET lead=excluded.lead,
                interest_status=excluded.interest_status""", (tid, lead, st, now))
        # DEMOTE-ONLY across the lead's other threads. Promoting cross-thread
        # would resurrect a thread the lead opted out of in another campaign, and
        # — because meetings attach to ONE thread — could re-open a sibling of a
        # thread whose call already happened.
        if st in config.EXCLUDED_STATUSES:
            conn.execute("UPDATE threads SET interest_status=? WHERE lead=?", (st, lead))
    conn.commit()
    n = sum(1 for v in latest.values() if v[0] == config.INTERESTED)
    return {"leads_seen": len(latest), "interested": n,
            "demoted": len(latest) - n}


def bootstrap(inst: Instantly, conn) -> dict:
    """One-off. Pull full history for interested threads only, so drafting has
    context from the very first notification."""
    res = sync_interest(inst, conn)
    tids = [r["thread_id"] for r in conn.execute(
        "SELECT thread_id FROM threads WHERE interest_status=? AND backfilled=0",
        (config.INTERESTED,))]
    done = 0
    for tid in tids:
        for e in inst.thread(tid):
            store.upsert_email(conn, e)
        store.recompute_thread(conn, tid)
        newest = conn.execute("""SELECT raw FROM emails WHERE thread_id=?
            ORDER BY timestamp_email DESC LIMIT 1""", (tid,)).fetchone()
        st = json.loads(newest["raw"]).get("i_status") if newest else None
        st = st if st in config.EXCLUDED_STATUSES else config.INTERESTED
        conn.execute("UPDATE threads SET backfilled=1, interest_status=? WHERE thread_id=?",
                     (st, tid))
        conn.commit()
        done += 1
        if done % 10 == 0:
            print(f"    backfilled {done}/{len(tids)}", flush=True)
    return {**res, "threads_backfilled": done, "requests": inst.request_count}


def poll(inst: Instantly, conn, since: datetime | None = None, backfill=True) -> dict:
    """Delta poll. `since` overrides the stored cursor — that is the replay hook."""
    now = datetime.now(timezone.utc)
    if since is None:
        cur = store.get_meta(conn, "cursor")
        since = (clock.parse(cur) if cur else now - timedelta(hours=1))
        since -= timedelta(minutes=config.POLL_OVERLAP_MIN)   # never resume exactly

    tracked = store.tracked_ids(conn)
    if not tracked:
        raise SystemExit("no tracked campaigns — run `resolve` first")

    new, skipped, touched = 0, 0, set()
    for e in inst.emails(min_timestamp_created=since.isoformat().replace("+00:00", "Z"),
                         sort_order="asc"):
        if e.get("campaign_id") not in tracked:
            skipped += 1
            continue
        if store.upsert_email(conn, e):
            new += 1
        if e.get("thread_id"):
            touched.add(e["thread_id"])

    fetched = 0
    if backfill:
        for tid in touched:
            row = conn.execute("SELECT backfilled FROM threads WHERE thread_id=?", (tid,)).fetchone()
            if row and row["backfilled"]:
                continue
            # Threads with no inbound have no history we lack — skip the request.
            has_reply = conn.execute(
                "SELECT 1 FROM emails WHERE thread_id=? AND is_outbound=0 LIMIT 1", (tid,)).fetchone()
            if not has_reply:
                continue
            for e in inst.thread(tid):          # one-time full history for drafting context
                store.upsert_email(conn, e)
            fetched += 1

    for tid in touched:
        store.recompute_thread(conn, tid)
        conn.execute("UPDATE threads SET backfilled=1 WHERE thread_id=?", (tid,))

    store.set_meta(conn, "cursor", now.isoformat())
    conn.commit()
    return {"new_emails": new, "skipped_other_campaigns": skipped,
            "threads_touched": len(touched), "threads_backfilled": fetched,
            "requests": inst.request_count}
