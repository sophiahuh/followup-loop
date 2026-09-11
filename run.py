#!/usr/bin/env python3
"""All loops. The poller is the only writer of thread state; everything else reads."""
import logging, sys, threading, time, traceback
from slack_bolt.adapter.socket_mode import SocketModeHandler
from followup import config, store, poller, notifier, slack_app
from followup.instantly import Instantly

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-9s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("run")


def every(seconds, name, fn):
    def loop():
        while True:
            try:
                r = fn()
                if r:
                    log.info("%s %s", name, r)
            except Exception:
                log.error("%s failed\n%s", name, traceback.format_exc())
            time.sleep(seconds)
    t = threading.Thread(target=loop, daemon=True, name=name)
    t.start()


def _gcal_sync(conn):
    from followup import gcal
    res = gcal.sync(conn, gcal.service())
    for r in conn.execute("SELECT DISTINCT thread_id FROM meetings"):
        store.recompute_thread(conn, r["thread_id"])
    conn.commit()
    return res


def main():
    ui_only = "--ui-only" in sys.argv
    cfg = config.slack()
    if not cfg.get("xoxb"):
        raise SystemExit("no ~/.slack_followup — phase 1 only: use `python3 cli.py poll`")
    conn = store.connect(); conn.execute("PRAGMA journal_mode=WAL")
    inst = Instantly()
    app = slack_app.make_app(store.connect())
    client = app.client
    dest = config.destination(cfg)

    if not ui_only:
        every(3600, "campaigns", lambda: poller.resolve_campaigns(inst, store.connect()))
        # Without this nothing in the running service ever writes interest_status,
        # so every lead flagged Interested after launch is stored, given a
        # deadline, and then silently filtered out of due_threads forever.
        every(3600, "interest", lambda: poller.sync_interest(inst, store.connect(), days=14))
        every(config.POLL_INTERVAL_SEC, "poll", lambda: poller.poll(inst, store.connect()))
        # Work-email aliases for new personal-domain leads, so a booking made under
        # a work address matches. ~2 credits per new lead, once. Skips itself
        # without a key or on any API failure.
        if config.crustdata_key():
            from followup import crustdata
            every(3600, "enrich", lambda: crustdata.enrich_new(store.connect()))
        every(300, "notify", lambda: notifier.notify_due(store.connect(), client, dest))
        every(300, "calendar", lambda: _gcal_sync(store.connect()))
        every(300, "recap", lambda: notifier.meeting_recap(store.connect(), client, dest))
    every(60, "sends", lambda: notifier.flush_sends(store.connect(), inst, client))
    every(90, "resolve", lambda: notifier.resolve_external_replies(store.connect(), client))

    log.info("dry_run=%s dest=%s — starting socket mode", config.DRY_RUN, dest)
    SocketModeHandler(app, cfg["xapp"]).start()


if __name__ == "__main__":
    main()
