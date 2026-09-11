#!/usr/bin/env python3
"""Invoke every Slack handler with a synthetic payload and a mock client.

Catches NameErrors, bad SQL, and cross-thread handles without a human clicking
buttons — twice now a broken handler reached the user because nothing exercised
these paths locally.
"""
import json, sys, threading
from followup import store, slack_app

class MockClient:
    def __init__(self): self.calls = []
    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            return {"ok": True, "ts": "1.1", "channel": "D0"}
        return call

def main():
    conn = store.connect()
    t = conn.execute("SELECT * FROM threads WHERE lead='tina@wenoexchange.com'").fetchone()
    tid = t["thread_id"]
    app, client, failures = slack_app.make_app(), MockClient(), []

    val = json.dumps({"t": tid, "s": "soft_nudge"})
    base = {"channel": {"id": "D0"}, "message": {"ts": "1.1"}, "trigger_id": "T0",
            "user": {"id": "U0"}}
    cases = [
        ("swap_direct_ask", {"action": {"value": json.dumps({"t": tid, "s": "direct_ask"})}}),
        ("edit",   {"action": {"value": val}}),
        ("send",   {"action": {"value": val}}),
        ("cancel_send", {"action": {"value": val}}),
        ("unhold", {"action": {"value": val}}),
        ("confirm_send", {"action": {"value": val}}),
        ("disp_had_call", {"action": {"action_id": "disp_had_call",
                                      "value": json.dumps({"e": "NOPE", "d": "had_call"})}}),
        ("disp_no_show", {"action": {"action_id": "disp_no_show",
                                     "value": json.dumps({"e": "NOPE", "d": "no_show"})}}),
        ("more",   {"action": {"value": json.dumps({"t": tid, "a": "stop"})}}),   # Dismiss button
    ]
    listeners = {}
    for aid, ls in app._listeners.items() if isinstance(app._listeners, dict) else []:
        listeners[aid] = ls

    for action_id, extra in cases:
        fn = None
        for l in app._listeners:
            m = getattr(l, "matchers", [])
            if any(action_id in str(getattr(x, "func", x)) or
                   action_id == getattr(x, "action_id", None) for x in m):
                fn = l.ack_function
        if fn is None:                      # fall back: match by registration order
            fn = next((l.ack_function for l in app._listeners
                       if action_id in str(l.ack_function.__code__.co_consts)
                       or l.ack_function.__name__ in action_id), None)
        if fn is None:
            failures.append(f"{action_id}: no listener found"); continue
        body = dict(base, **{k: v for k, v in extra.items() if k != "action"})
        kwargs = {"ack": lambda *a, **k: None, "body": body, "client": client,
                  "action": extra.get("action"), "view": None, "logger": None}
        try:
            fn(**{k: v for k, v in kwargs.items()
                  if k in fn.__code__.co_varnames[:fn.__code__.co_argcount]})
            print(f"  {action_id:16} ok")
        except Exception as e:
            failures.append(f"{action_id}: {type(e).__name__}: {e}")
            print(f"  {action_id:16} FAIL  {type(e).__name__}: {e}")

    # exercise from a worker thread too — that is how Bolt actually dispatches
    err = []
    def in_thread():
        try: slack_app.build_blocks(slack_app._conn(), tid, "soft_nudge")
        except Exception as e: err.append(e)
    th = threading.Thread(target=in_thread); th.start(); th.join()
    print(f"  {'worker thread':16} {'ok' if not err else 'FAIL ' + str(err[0])}")
    if err: failures.append(str(err[0]))

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
