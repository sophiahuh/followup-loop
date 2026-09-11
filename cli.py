#!/usr/bin/env python3
import argparse, sys
from datetime import datetime, timedelta, timezone
from followup import store, poller, clock, config
from followup.instantly import Instantly


def main():
    ap = argparse.ArgumentParser(prog="followup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("resolve", help="refresh the tracked campaign set")
    r = sub.add_parser("replay", help="re-run the poller over a past window")
    r.add_argument("--hours", type=int, default=72)
    sub.add_parser("poll", help="one delta poll from the stored cursor")
    sub.add_parser("bootstrap", help="pull every interested thread + full history")
    sub.add_parser("interest", help="refresh which leads are flagged interested")
    sub.add_parser("gcal-auth", help="one-time Google Calendar consent flow")
    sub.add_parser("gcal-sync", help="pull meetings and recompute thread states")
    sub.add_parser("status", help="what the store currently believes")
    d = sub.add_parser("due", help="threads past their deadline")
    d.add_argument("--limit", type=int, default=20)
    pv = sub.add_parser("preview", help="render what a Slack card would say, no Slack needed")
    pv.add_argument("--n", type=int, default=1)
    sw = sub.add_parser("sweep", help="post a batch of the older backlog the daily horizon suppresses")
    sw.add_argument("--days", type=int, default=30,
                    help=f"reach this far back (daily loop uses {config.MAX_OVERDUE_DAYS})")
    sw.add_argument("--limit", type=int, default=10, help="cards to post in this batch")
    sw.add_argument("--go", action="store_true", help="actually post; without it, list only")
    en = sub.add_parser("enrich", help="Crustdata: find work-email aliases for personal-domain leads")
    en.add_argument("--go", action="store_true", help="spend credits; without it, list only")
    en.add_argument("--limit", type=int, default=None, help="enrich only the first N (smoke test first)")
    rd = sub.add_parser("redraft", help="regenerate drafts on every open card and update it in Slack")
    rd.add_argument("--go", action="store_true", help="actually redraft; without it, list only")
    a = ap.parse_args()

    conn, inst = store.connect(), Instantly()

    if a.cmd == "resolve":
        res = poller.resolve_campaigns(inst, conn)
        print(f"campaigns seen {res['seen']}  tracked {res['tracked']}")
        for n in res["near_miss"]:
            print(f"  NEAR-MISS (untracked, looks like it should be): {n}")

    elif a.cmd == "gcal-auth":
        from followup import gcal
        svc = gcal.service(interactive=True)
        ev, tok = gcal.fetch(svc, days_back=7)
        print(f"authorised. {len(ev)} events on {gcal.CALENDAR_ID} in the last 7 days")
        print(f"token saved to {gcal.TOKEN_FILE}")

    elif a.cmd == "gcal-sync":
        from followup import gcal
        res = gcal.sync(conn, gcal.service())
        for k, v in res.items(): print(f"  {k:24} {v}")
        for r in conn.execute("SELECT DISTINCT thread_id FROM emails WHERE thread_id IS NOT NULL"):
            store.recompute_thread(conn, r["thread_id"])
        conn.commit()
        print("\n  thread meeting_state distribution:")
        for r in conn.execute("""SELECT COALESCE(meeting_state,'none') ms, state,
                                 COUNT(*) n FROM threads WHERE interest_status=1
                                 GROUP BY ms, state ORDER BY n DESC"""):
            print(f"    {r['ms']:16} {r['state']:12} {r['n']}")

    elif a.cmd in ("bootstrap", "interest"):
        fn = poller.bootstrap if a.cmd == "bootstrap" else poller.sync_interest
        for k, v in fn(inst, conn).items():
            print(f"  {k:24} {v}")

    elif a.cmd in ("replay", "poll"):
        since = datetime.now(timezone.utc) - timedelta(hours=a.hours) if a.cmd == "replay" else None
        if a.cmd == "replay":
            print(f"replaying {a.hours}h ...")
        for k, v in poller.poll(inst, conn, since=since).items():
            print(f"  {k:28} {v}")

    elif a.cmd == "status":
        q = lambda s: conn.execute(s).fetchone()[0]
        print(f"emails            {q('SELECT COUNT(*) FROM emails')}")
        print(f"threads           {q('SELECT COUNT(*) FROM threads')}")
        print(f"  interested      {q('SELECT COUNT(*) FROM threads WHERE interest_status=1')}")
        print(f"  awaiting us     {q('SELECT COUNT(*) FROM threads WHERE awaiting_us=1')}")
        print(f"tracked campaigns {q('SELECT COUNT(*) FROM campaigns WHERE tracked=1')}")
        print(f"cursor            {store.get_meta(conn,'cursor')}")

    elif a.cmd == "preview":
        preview(conn, a.n)

    elif a.cmd == "sweep":
        sweep(conn, a.days, a.limit, a.go)

    elif a.cmd == "redraft":
        redraft(conn, a.go)

    elif a.cmd == "enrich":
        from followup import crustdata, gcal
        rows = conn.execute("""SELECT thread_id, lead, enriched_at FROM threads
            WHERE interest_status=1 AND state='ACTIVE' AND lead IS NOT NULL
              AND COALESCE(meeting_state,'none')='none'
            GROUP BY lead""").fetchall()
        rows = [r for r in rows if r["lead"].split("@")[-1].lower() in config.FREE_EMAIL_DOMAINS]
        print(f"{len(rows)} interested leads on a personal domain with no calendar match")
        if a.limit:
            rows = rows[:a.limit]
        if not config.crustdata_key():
            print("no ~/.crustdata_key - nothing can run"); return
        res = crustdata.enrich_threads(conn, rows, go=a.go)
        print(res)
        if a.go:
            for cal in config.HOST_CALENDARS:
                store.set_meta(conn, f"gcal_token:{cal}", "")
            conn.commit()
            print("calendar resync with the new aliases:", gcal.sync(conn, gcal.service()))
            # Recompute EVERY thread of every lead with a meeting, not just the
            # thread the meeting row happens to point at - a lead's live thread is
            # often not the one the alias index mapped first.
            for r in conn.execute("""SELECT DISTINCT t.thread_id FROM threads t
                                     WHERE t.lead IN (SELECT lead FROM meetings WHERE lead IS NOT NULL)"""):
                store.recompute_thread(conn, r["thread_id"])
            conn.commit()
            new = conn.execute("SELECT lead, start_at, status FROM meetings WHERE match_type='enriched'").fetchall()
            print(f"{len(new)} meeting(s) now matched via an enriched work email")
            for m in new: print(f"   {m['lead']:34} {m['start_at'][:16]} {m['status']}")

    elif a.cmd == "due":
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        horizon = (now - timedelta(days=config.MAX_OVERDUE_DAYS)).isoformat()
        allrows = store.due_threads(conn, now.isoformat())
        rows = store.due_threads(conn, now.isoformat(), horizon)
        print(f"  ({len(allrows)-len(rows)} older than the {config.MAX_OVERDUE_DAYS}d "
              f"horizon — in the store, not notified)")
        print(f"{len(rows)} thread(s) due  ({config.SLA_BUSINESS_DAYS} business days)\n")
        for t in rows[:a.limit]:
            od = (now - clock.parse(t["due_at"])).days
            flag = "REPLIED, awaiting us" if t["awaiting_us"] else "no reply"
            print(f"  {(t['lead'] or '?')[:38]:38} {od}d overdue  {t['msg_count']}msg  {flag}")


def sweep(conn, days, limit, go):
    """The daily loop deliberately ignores anything past MAX_OVERDUE_DAYS so a
    months-old backlog never lands at once. This is the manual valve: it posts
    real cards, identical to the daily ones, in batches you ask for."""
    now = datetime.now(timezone.utc)
    daily = (now - timedelta(days=config.MAX_OVERDUE_DAYS)).isoformat()
    wide = (now - timedelta(days=days)).isoformat()
    fresh = {t["thread_id"] for t in store.due_threads(conn, now.isoformat(), daily)}
    # A thread the lead never replied on cannot be sent from (the fire-time guard
    # blocks it), so carding it is pure noise. Most are split-campaign duplicates.
    def replied(t):
        return conn.execute("SELECT 1 FROM emails WHERE thread_id=? AND is_outbound=0 LIMIT 1",
                            (t["thread_id"],)).fetchone() is not None
    rows = [t for t in store.due_threads(conn, now.isoformat(), wide)
            if t["thread_id"] not in fresh and replied(t)]
    print(f"{len(rows)} thread(s) between {config.MAX_OVERDUE_DAYS}d and {days}d overdue "
          f"(the daily loop already handles the newer {len(fresh)})\n")
    for t in rows[:limit]:
        od = (now - clock.parse(t["due_at"])).days
        print(f"  {(t['lead'] or '?')[:38]:38} {od:>3}d overdue  {t['msg_count']}msg")
    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more beyond this batch")
    if not go:
        print(f"\nnothing posted. add --go to send this batch of "
              f"{min(limit, len(rows))} to Slack")
        return
    from slack_sdk import WebClient
    from followup import notifier
    cfg = config.slack()
    res = notifier.notify_due(conn, WebClient(token=cfg["xoxb"]), config.destination(cfg),
                              limit=limit, horizon_days=days, force=True, only_replied=True)
    print(f"\nposted {res['posted']}")


def redraft(conn, go):
    """Re-run drafting on every card nobody has acted on yet, and update the card
    in place. For applying a new drafting rule to what is already posted, so the
    rule does not have to wait for the next cycle. Cards with a queued send, a
    cancel, or a completed send are left exactly as they are."""
    from followup import drafting, slack_app
    rows = conn.execute("""SELECT c.thread_id, c.channel, c.ts, t.lead FROM cards c
        JOIN threads t USING(thread_id)
        WHERE c.sent_at IS NULL AND c.cancelled=0 AND c.scheduled_for IS NULL
        ORDER BY c.posted_at""").fetchall()
    print(f"{len(rows)} open card(s)")
    if not go:
        for r in rows:
            print(f"  {r['lead']}")
        print("\nnothing changed. add --go to regenerate and update them in Slack")
        return
    from slack_sdk import WebClient
    client = WebClient(token=config.slack()["xoxb"])
    now = datetime.now(timezone.utc).isoformat()
    done = failed = 0
    for r in rows:
        t = conn.execute("SELECT * FROM threads WHERE thread_id=?", (r["thread_id"],)).fetchone()
        em = conn.execute("SELECT * FROM emails WHERE thread_id=? ORDER BY timestamp_email",
                          (r["thread_id"],)).fetchall()
        try:
            d = drafting.generate(t, em, conn)
        except Exception as e:
            print(f"  FAIL {r['lead']}: {e}"); failed += 1; continue
        conn.execute("DELETE FROM drafts WHERE thread_id=?", (r["thread_id"],))
        for k, body in d["drafts"].items():
            conn.execute("""INSERT INTO drafts(thread_id,strategy,body,recommended,generated_at)
                VALUES(?,?,?,?,?)""", (r["thread_id"], k, body,
                                       1 if k == d["recommended"] else 0, now))
        conn.execute("UPDATE threads SET hold_reason=?, human_reason=? WHERE thread_id=?",
                     (None if d["needs_followup"] else (d.get("hold_reason") or "already handled"),
                      (d.get("human_reason") or "needs you") if d.get("needs_human") else None,
                      r["thread_id"]))
        # Someone may have clicked Send, Cancel or Stop on this card while the
        # forty drafts ahead of it were generating. Re-check at the moment of the
        # update: a card that has been acted on keeps what the click rendered.
        live = conn.execute("""SELECT 1 FROM cards WHERE thread_id=? AND sent_at IS NULL
                               AND cancelled=0 AND scheduled_for IS NULL""",
                            (r["thread_id"],)).fetchone()
        if not live:
            conn.rollback()
            print(f"  skip {r['lead'][:34]:34} acted on during redraft, left alone")
            continue
        conn.execute("UPDATE cards SET strategy=? WHERE thread_id=?",
                     (d["recommended"], r["thread_id"]))
        conn.commit()
        try:
            client.chat_update(channel=r["channel"], ts=r["ts"], text=f"Follow-up: {r['lead']}",
                               blocks=slack_app.build_blocks(conn, r["thread_id"], d["recommended"]))
            done += 1
            print(f"  ok   {r['lead'][:34]:34} {d['recommended']}")
        except Exception as e:
            print(f"  FAIL {r['lead']} slack update: {e}"); failed += 1
    print(f"\nredrafted {done}, failed {failed}")


def preview(conn, n):
    from followup import drafting
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    horizon = (now - timedelta(days=config.MAX_OVERDUE_DAYS)).isoformat()
    rows = store.due_threads(conn, now.isoformat(), horizon) or \
           store.due_threads(conn, now.isoformat())
    if not rows:
        print("nothing due"); return
    for t in rows[:n]:
        emails = conn.execute("SELECT * FROM emails WHERE thread_id=? ORDER BY timestamp_email",
                              (t["thread_id"],)).fetchall()
        days = (now - clock.parse(t["clock_start_at"])).days
        print("=" * 68)
        print(f"{t['lead']}   {days} business-ish days since our last email   "
              f"{'REPLIED, awaiting you' if t['awaiting_us'] else 'no reply yet'}")
        print(f"thread {t['thread_id']}  {t['msg_count']} messages")
        print("=" * 68)
        try:
            d = drafting.generate(t, emails, conn)
        except Exception as e:
            print(f"  draft failed: {e}"); continue
        for k, body in d["drafts"].items():
            star = "  <-- recommended" if k == d["recommended"] else ""
            print(f"\n--- {drafting.STRATEGIES[k].upper()}{star}")
            print(body)
        print(f"\nwhy: {d.get('why','')}\n")


if __name__ == "__main__":
    main()
