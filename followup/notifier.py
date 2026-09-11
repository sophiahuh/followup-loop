"""Finds due threads, drafts, and posts one DM each. Never batched."""
import json, logging
from datetime import datetime, timedelta, timezone
from . import config, store, clock, drafting, slack_app

log = logging.getLogger("notifier")


def notify_due(conn, client, user_id: str, limit: int | None = None,
               horizon_days: int | None = None, force: bool = False,
               only_replied: bool = False) -> dict:
    """`horizon_days` and `force` exist for the manual backlog sweep: a sweep is
    asked for explicitly, so it may reach further back than the daily horizon and
    need not wait for the quiet-hours window."""
    now = datetime.now(timezone.utc)
    if clock.in_quiet_hours(now) and not force:
        return {"skipped": "quiet hours", "next": clock.next_delivery(now).isoformat()}

    limit = limit or config.NOTIFY_BATCH_LIMIT
    days = config.MAX_OVERDUE_DAYS if horizon_days is None else horizon_days
    horizon = (now - timedelta(days=days)).isoformat()
    rows = store.due_threads(conn, now.isoformat(), horizon)
    if only_replied:
        rows = [t for t in rows if conn.execute(
            "SELECT 1 FROM emails WHERE thread_id=? AND is_outbound=0 LIMIT 1",
            (t["thread_id"],)).fetchone()]
    # A lead worked by two campaigns has two threads. Only the one carrying the
    # most recent touchpoint is ever carded: a follow-up on the stale thread is
    # a follow-up to a conversation that moved. If the live thread is not due
    # yet, nothing posts, which is right.
    def _is_live_thread(t):
        newer = conn.execute("""SELECT 1 FROM emails e JOIN threads t2 ON t2.thread_id = e.thread_id
            WHERE t2.lead = ? AND t2.thread_id != ?
              AND e.timestamp_email > (SELECT MAX(timestamp_email) FROM emails WHERE thread_id = ?)
            LIMIT 1""", (t["lead"], t["thread_id"], t["thread_id"])).fetchone()
        return newer is None
    rows = [t for t in rows if _is_live_thread(t)]
    backlog = len(store.due_threads(conn, now.isoformat())) - len(rows)
    posted = 0
    for t in rows[:limit]:
        emails = conn.execute(
            "SELECT * FROM emails WHERE thread_id=? ORDER BY timestamp_email",
            (t["thread_id"],)).fetchall()
        try:
            data = drafting.generate(t, emails, conn)
        except Exception as e:                      # a bad draft must not lose the thread
            log.warning("draft failed for %s: %s", t["lead"], e)
            continue
        for k, body in data["drafts"].items():
            conn.execute("""INSERT INTO drafts(thread_id,strategy,body,recommended,generated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(thread_id,strategy) DO UPDATE SET
                body=excluded.body, recommended=excluded.recommended,
                generated_at=excluded.generated_at""",
                (t["thread_id"], k, body, 1 if k == data["recommended"] else 0,
                 now.isoformat()))
        conn.execute("UPDATE threads SET hold_reason=?, human_reason=? WHERE thread_id=?",
                     (None if data["needs_followup"] else (data.get("hold_reason") or "already handled"),
                      (data.get("human_reason") or "needs you") if data.get("needs_human") else None,
                      t["thread_id"]))
        conn.commit()
        r = client.chat_postMessage(
            channel=user_id, text=f"Follow-up: {t['lead']}",
            blocks=slack_app.build_blocks(conn, t["thread_id"], data["recommended"]))
        # A card is a NOTIFICATION, not a thread. Reusing the row without
        # resetting it carried cycle-1 state into cycle 2: a stale posted_at made
        # our own previous follow-up look like the lead replying, a stale sent_at
        # meant flush_sends silently never fired, and a stale override_at waived a
        # meeting nobody waived. Every re-post starts clean.
        conn.execute("""INSERT INTO cards(thread_id,channel,ts,strategy,posted_at)
            VALUES(?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET ts=excluded.ts,
            channel=excluded.channel, strategy=excluded.strategy,
            posted_at=excluded.posted_at, scheduled_for=NULL, sent_at=NULL,
            sent_email_id=NULL, body=NULL, cancelled=0, resolved_at=NULL,
            override_at=NULL""",
            (t["thread_id"], r["channel"], r["ts"], data["recommended"], now.isoformat()))
        slack_app.post_thread(client, conn, t["thread_id"], r["channel"], r["ts"])
        conn.execute("UPDATE threads SET notified_at=? WHERE thread_id=?",
                     (now.isoformat(), t["thread_id"]))
        conn.commit()
        posted += 1
    return {"due": len(rows), "posted": posted, "older_than_horizon": backlog}


def resolve_external_replies(conn, client) -> dict:
    """If an outbound appears on a thread after we posted its card — because you
    replied in the Instantly Unibox instead — retire the card on its own.

    The poller never learns this from us; it observes our send like any other
    email, which is why there is no state to keep in sync."""
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute("""SELECT * FROM cards WHERE sent_at IS NULL AND cancelled=0
                           AND resolved_at IS NULL""").fetchall()
    resolved = 0
    for c in rows:
        newer = conn.execute("""SELECT timestamp_email FROM emails WHERE thread_id=?
            AND is_outbound=1 AND timestamp_email > ? ORDER BY timestamp_email DESC LIMIT 1""",
            (c["thread_id"], c["posted_at"])).fetchone()
        if not newer:
            # Split-campaign duplicate: this thread has no reply from the lead, but
            # another thread of theirs does, and we have written on THAT one since
            # this card was posted. The conversation lives there; this card is
            # noise. Safe to retire on its own because a no-reply thread is
            # already blocked from sending at fire time - collapsing it can only
            # remove a card that could never have sent.
            try:
                sib = conn.execute("""
                    SELECT e.timestamp_email FROM emails e
                    JOIN threads t2 ON t2.thread_id = e.thread_id
                    JOIN threads t1 ON t1.lead = t2.lead
                    WHERE t1.thread_id = ? AND t2.thread_id != t1.thread_id
                      AND e.is_outbound = 1 AND e.timestamp_email > ?
                      AND NOT EXISTS (SELECT 1 FROM emails x WHERE x.thread_id = t1.thread_id
                                      AND x.is_outbound = 0)
                      AND EXISTS (SELECT 1 FROM emails y WHERE y.thread_id = t2.thread_id
                                  AND y.is_outbound = 0)
                    ORDER BY e.timestamp_email DESC LIMIT 1""",
                    (c["thread_id"], c["posted_at"])).fetchone()
                if sib:
                    when = clock.parse(sib["timestamp_email"]).astimezone(clock.NY)
                    client.chat_update(channel=c["channel"], ts=c["ts"], text="Handled on other thread",
                        blocks=[{"type": "context", "elements": [{"type": "mrkdwn",
                                 "text": f":white_check_mark: Followed up on their other campaign "
                                         f"thread at {when:%-I:%M%p on %b %-d} — nothing to send here."}]}])
                    conn.execute("UPDATE cards SET cancelled=1, resolved_at=? WHERE thread_id=?",
                                 (now, c["thread_id"]))
                    resolved += 1
            except Exception:
                log.exception("sibling-thread resolve failed on %s", c["thread_id"])
            continue
        when = clock.parse(newer["timestamp_email"]).astimezone(clock.NY)
        client.chat_update(channel=c["channel"], ts=c["ts"], text="Handled in Instantly",
            blocks=[{"type": "context", "elements": [{"type": "mrkdwn",
                     "text": f":white_check_mark: You replied in Instantly at "
                             f"{when:%-I:%M%p on %b %-d} — nothing needed here."}]}])
        conn.execute("UPDATE cards SET resolved_at=? WHERE thread_id=?", (now, c["thread_id"]))
        conn.execute("UPDATE threads SET notified_at=NULL WHERE thread_id=?", (c["thread_id"],))
        resolved += 1
    conn.commit()
    return {"resolved_externally": resolved}


def pending_dispositions(conn):
    """Meetings that have ended, were not cancelled or declined, have no
    disposition, and where the lead has no later meeting on the books.

    A rolling window, per Sophia (Sept 3): only calls from today, or from after
    4:30pm ET the previous day - i.e. anything since the last recap went out.
    Older ones are not carried forward; a backfill of weeks-old meetings should
    never land on the recap again."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    local = now_dt.astimezone(clock.NY)
    cutoff = (local - timedelta(days=1)).replace(hour=16, minute=30, second=0, microsecond=0)
    since = cutoff.astimezone(timezone.utc).isoformat()
    rows = conn.execute("""
        SELECT m.* FROM meetings m
        WHERE m.disposition IS NULL AND m.status='confirmed'
          AND COALESCE(m.response_status,'') != 'declined'
          AND m.start_at IS NOT NULL AND m.start_at <= ? AND m.start_at >= ?
          -- One question per thread: ask about the most recent meeting that has
          -- actually happened. Previously any LATER meeting hid it, including a
          -- future booking — which made an un-answered past call permanently
          -- un-dispositionable, the other half of the same defect.
          AND NOT EXISTS (SELECT 1 FROM meetings m2 WHERE m2.thread_id=m.thread_id
                          AND m2.status='confirmed' AND m2.start_at > m.start_at
                          AND m2.start_at <= ?)
        ORDER BY m.start_at DESC""", (now, since, now)).fetchall()
    return [r for r in rows
            if (r["lead"] or "").split("@")[-1] not in config.CUSTOMER_DOMAINS]


def recap_blocks(conn):
    rows = pending_dispositions(conn)
    if not rows:
        return None, 0
    b = [{"type": "section", "text": {"type": "mrkdwn",
          "text": f"*{len(rows)} meeting{'s' if len(rows) != 1 else ''} to close out.*\n"
                  "Nothing else needs deciding — did they turn up?"}}]
    for m in rows[:12]:
        when = clock.parse(m["start_at"]).astimezone(clock.NY) if m["start_at"] else None
        b += [{"type": "divider"},
              {"type": "section", "text": {"type": "mrkdwn",
               "text": f"*{m['lead']}*\n{m['summary'] or 'meeting'} · "
                       f"{when:%a %b %-d, %-I:%M%p}" if when else str(m["summary"])}},
              {"type": "actions", "elements": [
                {"type": "button", "action_id": "disp_had_call", "style": "primary",
                 "text": {"type": "plain_text", "text": "Had the call"},
                 "value": json.dumps({"e": m["event_id"], "d": "had_call"})},
                {"type": "button", "action_id": "disp_no_show",
                 "text": {"type": "plain_text", "text": "No-show"},
                 "value": json.dumps({"e": m["event_id"], "d": "no_show"})}]}]
    if len(rows) > 12:
        b.append({"type": "context", "elements": [{"type": "mrkdwn",
                  "text": f"…and {len(rows) - 12} more, next time"}]})
    return b, len(rows)


def meeting_recap(conn, client, user_id: str, force: bool = False) -> dict:
    """Weekdays at 16:30 ET — before the Friday 17:00 cutoff."""
    now = datetime.now(timezone.utc)
    local = now.astimezone(clock.NY)
    today = local.strftime("%Y-%m-%d")
    if not force:
        if local.weekday() >= 5 or local.hour < 16 or (local.hour == 16 and local.minute < 30):
            return {}
        if store.get_meta(conn, "last_recap") == today:
            return {}
    blocks, n = recap_blocks(conn)
    store.set_meta(conn, "last_recap", today); conn.commit()
    if not blocks:
        return {"recap": "nothing pending"}
    r = client.chat_postMessage(channel=user_id, text=f"{n} meetings to close out",
                                blocks=blocks)
    store.set_meta(conn, "recap_ts", r["ts"]); store.set_meta(conn, "recap_ch", r["channel"])
    conn.commit()
    return {"recap_posted": n}


_LIVE_ACCOUNTS = {"at": 0.0, "set": None}


def live_accounts(inst):
    """Sending accounts that currently exist in Instantly, cached 10 minutes.
    Returns None whenever it cannot be trusted - fetch failed, or the list is
    implausibly short - and callers must treat None as "don't check". A guard
    that can only ever skip itself cannot block a good send."""
    import time
    now = time.monotonic()
    if _LIVE_ACCOUNTS["set"] is not None and now - _LIVE_ACCOUNTS["at"] < 600:
        return _LIVE_ACCOUNTS["set"]
    try:
        accts = {(a.get("email") or "").lower() for a in inst.paginate("/accounts", limit=100)}
    except Exception as exc:
        log.warning("live_accounts unavailable, not checking: %s", exc)
        return None
    if len(accts) < 50:                       # partial page or odd response: don't trust it
        return None
    _LIVE_ACCOUNTS.update(at=now, set=accts)
    return accts


def flush_sends(conn, inst, client) -> dict:
    """Fire anything whose scheduled moment has arrived and wasn't cancelled."""
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute("""SELECT * FROM cards WHERE scheduled_for IS NOT NULL
        AND scheduled_for <= ? AND sent_at IS NULL AND cancelled=0""", (now,)).fetchall()
    sent = 0
    blocked = 0
    for c in rows:
      try:
        # THE fire-time guard. A queued send can sit 20-70 hours: the lead can
        # book, take the call, be stopped by hand, or be answered in the Unibox
        # in that window. Everything is re-derived here, not trusted from click
        # time. An override waives an upcoming meeting only — never a call that
        # actually happened.
        reason = store.send_block_reason(conn, c["thread_id"], override=bool(c["override_at"]))
        if reason:
            conn.execute("UPDATE cards SET cancelled=1, resolved_at=? WHERE thread_id=?",
                         (now, c["thread_id"]))
            conn.commit()
            blocked += 1
            log.info("BLOCKED send on %s: %s", c["thread_id"], reason)
            client.chat_update(channel=c["channel"], ts=c["ts"], text="Not sent",
                blocks=[{"type": "context", "elements": [{"type": "mrkdwn",
                 "text": ":no_entry: Not sent — " + reason + "."}]}])
            continue
        # Reply to the LEAD's newest message, not simply the newest message.
        # Instantly derives the recipient from the sender of reply_to_uuid, so
        # pointing it at our own last email mails us instead of the lead — the
        # thread is right, the recipient is us. Verified: a send targeting our
        # own message came back with to == our own eaccount. Outbound is only a
        # fallback for the (rare) thread with no inbound at all.
        last = conn.execute("""SELECT id, eaccount, subject, is_outbound FROM emails
            WHERE thread_id=? ORDER BY is_outbound ASC, timestamp_email DESC LIMIT 1""",
            (c["thread_id"],)).fetchone()
        if last is None:
            continue
        # A reply cannot leave a mailbox that no longer exists. Brian's thread was
        # on sarah@buildcrustdata.com, since deleted from Instantly, and the send
        # failed with a 404 that read as "did not confirm". Say what it is.
        live = live_accounts(inst)
        if live is not None and (last["eaccount"] or "").lower() not in live:
            conn.execute("UPDATE cards SET cancelled=1, resolved_at=?, send_error=? WHERE thread_id=?",
                         (now, f"sending account {last['eaccount']} no longer exists", c["thread_id"]))
            conn.commit()
            blocked += 1
            log.info("BLOCKED send on %s: account %s gone", c["thread_id"], last["eaccount"])
            client.chat_update(channel=c["channel"], ts=c["ts"], text="Not sent", blocks=[
                {"type": "context", "elements": [{"type": "mrkdwn",
                 "text": f":no_entry: Not sent — this thread's sending account "
                         f"`{last['eaccount']}` has been removed from Instantly, so a reply "
                         f"can't go out from it. Send this one by hand from a live account."}]}])
            continue
        # No inbound anywhere on the thread means there is nothing to reply to and
        # the mail would come straight back to us. Two of the currently cardable
        # threads are like this. Block rather than send into a mirror.
        if last["is_outbound"]:
            conn.execute("UPDATE cards SET cancelled=1, resolved_at=? WHERE thread_id=?",
                         (now, c["thread_id"]))
            conn.commit()
            blocked += 1
            log.info("BLOCKED send on %s: no inbound to reply to", c["thread_id"])
            client.chat_update(channel=c["channel"], ts=c["ts"], text="Not sent", blocks=[
                {"type": "context", "elements": [{"type": "mrkdwn",
                 "text": ":no_entry: Not sent — this lead has never replied on this "
                         "thread, so there is no message to reply to. Send it from "
                         "Instantly instead."}]}])
            continue

        # CLAIM THE CARD BEFORE THE NETWORK CALL. A timeout does not mean the mail
        # was not sent — Instantly can accept it and lose the response — so a retry
        # is a duplicate, not a recovery. Writing sent_at first makes the next pass
        # skip this row whatever happens. At-most-once, deliberately, because the
        # failure we can tolerate is a follow-up that did not go out; the one we
        # cannot is a lead getting the same email nine times.
        claimed = conn.execute("""UPDATE cards SET sent_at=? WHERE thread_id=?
                                  AND sent_at IS NULL AND cancelled=0""",
                               (now, c["thread_id"]))
        conn.commit()
        if claimed.rowcount == 0:
            continue

        if config.DRY_RUN:
            log.info("DRY RUN would reply on %s as %s:\n%s",
                     c["thread_id"], last["eaccount"], c["body"])
            conn.execute("UPDATE cards SET sent_email_id='DRY_RUN' WHERE thread_id=?",
                         (c["thread_id"],))
        else:
            try:
                res = inst.reply(last["eaccount"], last["id"], last["subject"] or "", c["body"])
                conn.execute("UPDATE cards SET sent_email_id=? WHERE thread_id=?",
                             (res.get("id"), c["thread_id"]))
            except Exception as exc:
                # Never retry. Say plainly that we do not know, and let her look.
                # The error text is kept on the card row and shown on the card:
                # the process log is lost on restart, and "did not confirm" with
                # no reason is not something anyone can act on.
                err = f"{type(exc).__name__}: {exc}"[:400]
                conn.execute("UPDATE cards SET sent_email_id='UNCONFIRMED', send_error=? "
                             "WHERE thread_id=?", (err, c["thread_id"]))
                conn.commit()
                log.error("send UNCONFIRMED on %s: %s", c["thread_id"], err)
                try:
                    client.chat_update(channel=c["channel"], ts=c["ts"], text="Send unconfirmed",
                        blocks=[{"type": "context", "elements": [{"type": "mrkdwn",
                         "text": ":warning: Send did not confirm — it may or may not have gone "
                                 "out. Check the Instantly Unibox before resending.\n"
                                 f"`{err[:160]}`"}]}])
                except Exception:
                    pass
                continue

        from . import examples
        examples.record_sent(conn, c["thread_id"], c["body"])
        conn.commit()
        tag = "Would have sent (dry run)" if config.DRY_RUN else "Sent"
        by = f" by <@{c['actor']}>" if c["actor"] else ""
        try:
            client.chat_update(channel=c["channel"], ts=c["ts"], text=tag, blocks=[
                {"type": "context", "elements": [{"type": "mrkdwn",
                 "text": f":white_check_mark: {tag}{by} · "
                         f"{clock.parse(now).astimezone(clock.NY):%-I:%M%p}"}]}])
        except Exception as exc:
            log.warning("card update failed on %s: %s", c["thread_id"], exc)
        sent += 1
      except Exception:
        # A card that blows up must not take the rest of the queue down with it.
        log.exception("flush_sends failed on %s", c["thread_id"])
        continue

    return {"sent": sent, "blocked": blocked}
