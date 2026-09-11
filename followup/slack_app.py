"""Slack surface. The draft is in the message — no Draft button, no modal to open.
Socket Mode, so no public endpoint is needed."""
import json, re, threading
from datetime import datetime, timezone
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from . import config, store, clock, drafting

STRAT = drafting.STRATEGIES


def _fmt(dt):
    return dt.astimezone(clock.NY).strftime("%-I:%M%p").lower()


def clean_body(raw: dict) -> str:
    from .drafting import extract_body
    return extract_body(raw)          # already quote-stripped


def post_thread(client, conn, thread_id: str, channel: str, ts: str) -> int:
    """Post the whole conversation under the card as it is created — newest
    first, so the original cold email sits at the bottom. One message per email."""
    rows = conn.execute("""SELECT * FROM emails WHERE thread_id=?
                           ORDER BY timestamp_email DESC""", (thread_id,)).fetchall()
    for r in rows:
        who = "You" if r["is_outbound"] else (r["from_email"] or "them")
        when = r["timestamp_email"][:16].replace("T", " ")
        text = clean_body(json.loads(r["raw"])) or "_(no body)_"
        blocks = [{"type": "context", "elements":
                   [{"type": "mrkdwn", "text": f"*{who}* · {when}"}]}]
        for i in range(0, min(len(text), 8000), 2800):
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": text[i:i + 2800]}})
        client.chat_postMessage(channel=channel, thread_ts=ts,
                                text=f"{who} · {when}", blocks=blocks)
    return len(rows)


ADDR_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def redirect_address(conn, t) -> str | None:
    """An address the lead named that we are not the one writing to.

    This matters more than it looks: a reply goes back to whoever sent the message
    we are replying to, so answering in-thread reaches the address they asked us
    NOT to use. The address has to be changed in Instantly first — there is no way
    to redirect it from here.
    """
    from . import config
    from .drafting import extract_body
    known = {(t["lead"] or "").lower()}
    for r in conn.execute("SELECT DISTINCT from_email FROM emails WHERE thread_id=?",
                          (t["thread_id"],)):
        if r["from_email"]:
            known.add(r["from_email"].lower())
    for m in conn.execute("""SELECT raw FROM emails WHERE thread_id=? AND is_outbound=0
                             ORDER BY timestamp_email""", (t["thread_id"],)):
        for a in ADDR_RE.findall(extract_body(json.loads(m["raw"]))):
            al = a.lower()
            if al not in known and config.INTERNAL_DOMAIN_TOKEN not in al.split("@")[-1]:
                return al
    return None


def personal_address(t) -> bool:
    """We don't prospect people at a personal address — the action is to find a
    work email or reach them another way, so a draft to that address is the wrong
    thing to hand over. 23% of the due backlog sits here."""
    from . import config
    return (t["lead"] or "").split("@")[-1].lower() in config.FREE_EMAIL_DOMAINS


def meeting_signal(conn, t):
    """Any hint at all that this person may have been on a call.

    Deliberately over-inclusive. Detection can never be perfect — a lead can book
    under an address that appears in no email — so the guarantee is not that the
    matcher is right, it is that a thread carrying ANY signal cannot be sent to in
    one click. A false signal costs a few seconds; a false send costs the deal.
    """
    st, m = store.meeting_state(conn, t["thread_id"])
    if m is not None:
        when = (m["start_at"] or "")[:10]
        label = {store.MEET_UPCOMING: f"has a meeting booked for {when}",
                 store.MEET_JUST_HAPPENED: f"met on {when} — outcome not recorded yet",
                 store.MEET_HAD_CALL: f"already had a call on {when}",
                 store.MEET_CANCELLED: f"meeting on {when} was cancelled",
                 store.MEET_DECLINED: f"declined the invite for {when}"}.get(st)
        if st in (store.MEET_UPCOMING, store.MEET_JUST_HAPPENED, store.MEET_HAD_CALL):
            # A name-based match is an inference, not an address hit. Say so, and
            # say which address it was booked under, so Sophia can check it is them.
            if (m["match_type"] or "") == "name":
                label += (f" — booked as {m['matched_address']}, matched to this lead by "
                          f"name, check it's them")
            elif (m["match_type"] or "") == "enriched":
                label += (f" — booked as {m['matched_address']}, a work address Crustdata "
                          f"lists for this person ({t['enrich_note'] or 'name unknown'})")
            return label, m
    em = conn.execute("SELECT * FROM emails WHERE thread_id=? ORDER BY timestamp_email",
                      (t["thread_id"],)).fetchall()
    from .motion import for_thread
    if for_thread(conn, t["thread_id"], em) == "they_booked":
        return "said in the thread that they booked — no calendar event found", None
    return None, None


def exceptions(conn, t) -> list[str]:
    """Only things a normal card would not already tell you.

    Returns (actionable, context). Age and who-wrote-last are visible from the
    preview and the thread below, so they are not exceptions. Actionable flags
    change what you should write and get a warning each, capped at two; context
    flags are worth knowing but do not change the action, so they collapse into
    one unadorned line. Without the split, four equal warnings push the draft off
    screen and none of them reads as urgent.
    """
    from .motion import for_thread
    act, ctx, tid = [], [], t["thread_id"]
    # The model flags threads it should not answer alone — unanswered pricing,
    # contractual terms, anything it would have to invent. That signal was being
    # computed and then thrown away, so those cards looked ordinary.
    if "human_reason" in t.keys() and t["human_reason"]:
        act.append(f"needs you: {t['human_reason']}")
    # A cancelled or declined meeting is context, not a send blocker — the
    # meeting guard deliberately lets these through — but it changes what you
    # write, so it belongs on the card.
    # Context, not a warning: it does not need Sophia, it lets her check the
    # draft makes sense against what actually happened on the calendar.
    mst, mrow = store.meeting_state(conn, tid)
    if mrow is not None and mst in (store.MEET_CANCELLED, store.MEET_DECLINED,
                                    store.MEET_WE_CANCELLED, store.MEET_INVITE_WITHDRAWN):
        when = (mrow["start_at"] or "")[:10]
        tool = (mrow["booking_tool"] or "manual") if "booking_tool" in mrow.keys() else "manual"
        if mst == store.MEET_CANCELLED and tool in ("hubspot", "calendly"):
            # Booked through a tool: either side can cancel from the confirmation
            # email, so the side is unknown. The draft assumes them; the card says so.
            line = f"the {when} {tool.title()} booking was cancelled - can't tell which side"
        else:
            line = {store.MEET_CANCELLED: f"they cancelled their {when} invite",
                    store.MEET_DECLINED: f"declined the invite for {when}",
                    store.MEET_WE_CANCELLED: f"WE cancelled the {when} meeting they had accepted",
                    store.MEET_INVITE_WITHDRAWN: f"we sent an invite for {when} that was never "
                                                 f"accepted, and withdrew it"}[mst]
        if (mrow["match_type"] or "") == "name":
            line += f" (booked as {mrow['matched_address']}, matched by name)"
        elif (mrow["match_type"] or "") == "enriched":
            line += f" (booked as {mrow['matched_address']}, via Crustdata)"
        ctx.append(line)
    # Instantly scopes a thread id to a campaign, so a lead worked by two
    # campaigns has two threads. The lead is flagged interested on both, but the
    # reply sits on only one — the other looks like silence that it is not.
    if not conn.execute("SELECT 1 FROM emails WHERE thread_id=? AND is_outbound=0 LIMIT 1",
                        (tid,)).fetchone():
        if conn.execute("""SELECT 1 FROM emails e JOIN threads t2 USING(thread_id)
                           WHERE t2.lead=? AND t2.thread_id != ? AND e.is_outbound=0
                           LIMIT 1""", (t["lead"], tid)).fetchone():
            act.append("they replied on another campaign's thread, not this one — "
                       "follow up there instead")
    em = conn.execute("SELECT * FROM emails WHERE thread_id=? ORDER BY timestamp_email",
                      (tid,)).fetchall()
    m = for_thread(conn, tid, em)
    if m == "they_sent_link":
        act.append("they sent their calendar link and nobody booked")
    elif m == "we_promised_invite":
        act.append("we promised a calendar invite and never sent it")

    alt = {r["from_email"] for r in em
           if not r["is_outbound"] and r["from_email"] and r["from_email"] != t["lead"]}
    if alt:
        act.append(f"replying from {', '.join(sorted(alt))}, not {t['lead']}")

    others = conn.execute("""SELECT COUNT(*) n FROM threads WHERE lead=? AND state='ACTIVE'
                             AND thread_id != ?""", (t["lead"], tid)).fetchone()["n"]
    if others:
        ctx.append(f"{others} other open thread{'s' if others > 1 else ''}")

    anchor = clock.parse(t["clock_start_at"])
    if anchor and (datetime.now(timezone.utc) - anchor).days >= 14:
        ctx.append(f"quiet for {(datetime.now(timezone.utc) - anchor).days} days")
    # act = changes what you write; ctx = worth knowing, folded into one quiet line
    return act[:2], ctx


def build_blocks(conn, thread_id: str, strategy: str) -> list:
    t = conn.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
    d = conn.execute("SELECT body FROM drafts WHERE thread_id=? AND strategy=?",
                     (thread_id, strategy)).fetchone()

    last = conn.execute("""SELECT timestamp_email, raw, is_outbound FROM emails
        WHERE thread_id=? ORDER BY timestamp_email DESC LIMIT 1""", (thread_id,)).fetchone()
    quote = ""
    if last:
        who = "You wrote" if last["is_outbound"] else "They wrote"
        full = clean_body(json.loads(last["raw"]))
        shown = full[:600] + ("…" if len(full) > 600 else "")
        quote = f"*{who}, {last['timestamp_email'][:10]}*\n>>> {shown}"

    others = [k for k in STRAT if k != strategy]
    if t["hold_reason"]:
        # The model judged that nudging here would hurt. Nothing is drafted into
        # the message; the drafts stay one click away in case it judged wrong.
        return [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*{t['lead']}*  ·  <{config.INSTANTLY_THREAD_URL.format(thread_id=t['thread_id'])}"
                 f"|open in Instantly>\n*No follow-up needed* — {t['hold_reason']}"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": quote}]},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "unhold",
                 "text": {"type": "plain_text", "text": "Follow up anyway"},
                 "value": json.dumps({"t": thread_id, "s": strategy})},
                {"type": "button", "action_id": "more", "text":
                 {"type": "plain_text", "text": "Dismiss"},
                 "value": json.dumps({"t": thread_id, "a": "stop"})}]}]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*{t['lead']}*  ·  <{config.INSTANTLY_THREAD_URL.format(thread_id=t['thread_id'])}"
                 f"|open in Instantly>"}},
    ]
    signal, _mrow = meeting_signal(conn, t)
    act, ctx = exceptions(conn, t)
    redirect = redirect_address(conn, t)
    if redirect:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": f":envelope_with_arrow:  *they asked us to use `{redirect}` — "
                               f"add it in Instantly first, a reply here goes to the old address*"}]})
    if signal:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": f":no_entry:  *{signal}*"}]})
    for e in act:
        blocks.append({"type": "context", "elements":
                       [{"type": "mrkdwn", "text": f":warning:  *{e}*"}]})
    if ctx:
        blocks.append({"type": "context", "elements":
                       [{"type": "mrkdwn", "text": "  ·  ".join(ctx)}]})
    if quote:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": quote}]})
    blocks += [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": d["body"] if d else "_no draft_"}},
        {"type": "actions", "elements":
            ([{"type": "button", "action_id": "confirm_send", "style": "danger",
               "text": {"type": "plain_text", "text": "Send anyway…"},
               "value": json.dumps({"t": thread_id, "s": strategy})}] if signal else
             [{"type": "button", "action_id": "send",
               **({"style": "primary"} if not redirect else {}),
               "text": {"type": "plain_text",
                        # No time on the button: it fires two minutes after the click,
                        # so a time computed at render would be stale within minutes.
                        "text": ("Send" if not redirect
                                 else "Send anyway")},
               "value": json.dumps({"t": thread_id, "s": strategy})}]) +
            [
             {"type": "button", "action_id": "edit",
              "text": {"type": "plain_text", "text": "Edit"},
              "value": json.dumps({"t": thread_id, "s": strategy})}]
            + [{"type": "button", "action_id": f"swap_{k}",
                "text": {"type": "plain_text", "text": STRAT[k]},
                "value": json.dumps({"t": thread_id, "s": k})} for k in others]
            # Snooze removed: it wrote due_at directly, which recompute_thread
            # then recalculated from the anchor within minutes — clearing
            # notified_at while erasing the delay, so the card came back SOONER.
            # Stop stays in an overflow so the destructive action is not one tap.
            + [{"type": "overflow", "action_id": "more",
                "options": [
                  {"text": {"type": "plain_text", "text": "Stop this loop"},
                   "value": json.dumps({"t": thread_id, "a": "stop"})}]}]},
    ]
    return blocks


_local = threading.local()


def _conn():
    """Bolt dispatches every interaction on a worker thread, and a SQLite handle
    cannot cross threads. One connection per thread, created on first use."""
    if not hasattr(_local, "conn"):
        _local.conn = store.connect()
    return _local.conn


def make_app(_ignored=None) -> App:
    cfg = config.slack()
    app = App(token=cfg["xoxb"], token_verification_enabled=False)

    def repost(client, thread_id, strategy, channel, ts):
        conn = _conn()
        client.chat_update(channel=channel, ts=ts, text="Follow-up",
                           blocks=build_blocks(conn, thread_id, strategy))
        conn.execute("UPDATE cards SET strategy=? WHERE thread_id=?", (strategy, thread_id))
        conn.commit()

    for key in STRAT:
        @app.action(f"swap_{key}")
        def swap(ack, body, client, action):
            ack()
            conn = _conn()
            v = json.loads(action["value"])
            repost(client, v["t"], v["s"], body["channel"]["id"], body["message"]["ts"])

    @app.action("unhold")
    def unhold(ack, body, client, action):
        ack()
        conn = _conn()
        v = json.loads(action["value"])
        conn.execute("UPDATE threads SET hold_reason=NULL WHERE thread_id=?", (v["t"],))
        conn.commit()
        repost(client, v["t"], v["s"], body["channel"]["id"], body["message"]["ts"])

    def _dispose(client, body, action, verdict):
        """Shared by both disposition buttons. Bolt injects only parameters it
        recognises, so the verdict cannot ride in as a closure default — it is
        passed explicitly by two thin, separately-named handlers."""
        conn = _conn()
        from . import notifier
        v = json.loads(action["value"])
        conn.execute("UPDATE meetings SET disposition=?, dispositioned_at=? WHERE event_id=?",
                     (verdict, datetime.now(timezone.utc).isoformat(), v["e"]))
        row = conn.execute("SELECT thread_id FROM meetings WHERE event_id=?",
                           (v["e"],)).fetchone()
        conn.commit()
        if row:
            store.recompute_thread(conn, row["thread_id"])   # a no-show reopens it
            conn.commit()
        blocks, n = notifier.recap_blocks(conn)
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                           text=f"{n} to close out" if n else "All closed out",
                           blocks=blocks or [{"type": "context", "elements":
                             [{"type": "mrkdwn",
                               "text": ":white_check_mark: All meetings closed out."}]}])

    @app.action("disp_had_call")
    def disp_had_call(ack, body, client, action):
        ack()
        _dispose(client, body, action, "had_call")

    @app.action("disp_no_show")
    def disp_no_show(ack, body, client, action):
        ack()
        _dispose(client, body, action, "no_show")

    @app.action("confirm_send")
    def confirm_send(ack, body, client, action):
        """Never one-click. States what we found and makes the override explicit."""
        ack()
        conn = _conn()
        v = json.loads(action["value"])
        t = conn.execute("SELECT * FROM threads WHERE thread_id=?", (v["t"],)).fetchone()
        signal, _m = meeting_signal(conn, t)
        row = conn.execute("SELECT body FROM drafts WHERE thread_id=? AND strategy=?",
                           (v["t"], v["s"])).fetchone()
        client.views_open(trigger_id=body["trigger_id"], view={
            "type": "modal", "callback_id": "override_send",
            "private_metadata": json.dumps({**v, "ch": body["channel"]["id"],
                                            "ts": body["message"]["ts"]}),
            "title": {"type": "plain_text", "text": "Confirm send"},
            "submit": {"type": "plain_text", "text": "Send anyway"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f":no_entry: *{t['lead']} {signal or 'may have met us'}.*\n\n"
                         f"Sending a follow-up to someone who has been on a call is the "
                         f"one thing this tool exists to prevent. Continue only if you "
                         f"know the meeting did not happen."}},
                {"type": "divider"},
                {"type": "input", "block_id": "b", "label":
                 {"type": "plain_text", "text": "Message"},
                 "element": {"type": "plain_text_input", "action_id": "body",
                             "multiline": True,
                             "initial_value": row["body"] if row else ""}}]})

    @app.view("override_send")
    def override_send(ack, body, client, view):
        ack()
        conn = _conn()
        m = json.loads(view["private_metadata"])
        text = view["state"]["values"]["b"]["body"]["value"]
        schedule(conn, client, m["t"], m["s"], text, m["ch"], m["ts"], override=True,
                 actor=body["user"]["id"])

    @app.action("send")
    def send(ack, body, client, action):
        ack()
        conn = _conn()
        v = json.loads(action["value"])
        row = conn.execute("SELECT body FROM drafts WHERE thread_id=? AND strategy=?",
                           (v["t"], v["s"])).fetchone()
        schedule(conn, client, v["t"], v["s"], row["body"],
                 body["channel"]["id"], body["message"]["ts"],
                 actor=body["user"]["id"])

    @app.action("edit")
    def edit(ack, body, client, action):
        ack()
        conn = _conn()
        v = json.loads(action["value"])
        row = conn.execute("SELECT body FROM drafts WHERE thread_id=? AND strategy=?",
                           (v["t"], v["s"])).fetchone()
        client.views_open(trigger_id=body["trigger_id"], view={
            "type": "modal", "callback_id": "edit_send",
            "private_metadata": json.dumps({**v, "ch": body["channel"]["id"],
                                            "ts": body["message"]["ts"]}),
            "title": {"type": "plain_text", "text": "Edit reply"},
            "submit": {"type": "plain_text", "text": "Schedule send"},
            "blocks": [{"type": "input", "block_id": "b", "label":
                        {"type": "plain_text", "text": "Message"},
                        "element": {"type": "plain_text_input", "action_id": "body",
                                    "multiline": True, "initial_value": row["body"]}}]})

    @app.view("edit_send")
    def edit_submit(ack, body, client, view):
        ack()
        conn = _conn()
        m = json.loads(view["private_metadata"])
        text = view["state"]["values"]["b"]["body"]["value"]
        schedule(conn, client, m["t"], m["s"], text, m["ch"], m["ts"],
                 actor=body["user"]["id"])

    @app.action("more")
    def more(ack, body, client, action):
        ack()
        conn = _conn()
        # "more" is fired by two different element types: the overflow menu
        # (value under selected_option) and the plain Dismiss button on a hold
        # card (value at top level). Accept either shape.
        v = json.loads((action.get("selected_option") or {}).get("value")
                       or action["value"])
        conn.execute("UPDATE threads SET state='STOPPED' WHERE thread_id=?", (v["t"],))
        msg = f":black_square_for_stop: Loop stopped by <@{body['user']['id']}>."
        conn.commit()
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                           text=msg, blocks=[{"type": "section",
                           "text": {"type": "mrkdwn", "text": msg}}])

    @app.action("cancel_send")
    def cancel(ack, body, client, action):
        ack()
        conn = _conn()
        v = json.loads(action["value"])
        conn.execute("UPDATE cards SET cancelled=1 WHERE thread_id=?", (v["t"],))
        conn.commit()
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                           text="Cancelled", blocks=build_blocks(conn, v["t"], v["s"]))
    return app


def already_handled(conn, client, thread_id, channel, ts, override=False) -> bool:
    """Refuse to queue if anything now blocks this send. Evaluated at CLICK time —
    the card's buttons were rendered when it was posted, and everything the guard
    depends on can have changed since."""
    reason = store.send_block_reason(conn, thread_id, override=override)
    if not reason:
        return False
    conn.execute("UPDATE cards SET resolved_at=?, cancelled=1 WHERE thread_id=?",
                 (datetime.now(timezone.utc).isoformat(), thread_id))
    conn.commit()
    client.chat_update(channel=channel, ts=ts, text="Not sent", blocks=[
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": ":no_entry: Not sent — " + reason + "."}]}])
    return True


def schedule(conn, client, thread_id, strategy, text, channel, ts, override=False,
             actor=None):
    """Queue the send. Because it's scheduled rather than immediate, the Cancel
    button below is a real undo window — which is the whole point."""
    if already_handled(conn, client, thread_id, channel, ts, override=override):
        return
    hours = [int(r["timestamp_email"][11:13]) for r in conn.execute(
        """SELECT e.timestamp_email FROM emails e JOIN threads t USING(thread_id)
           WHERE t.thread_id=? AND e.is_outbound=0""", (thread_id,))]
    target = clock.send_target(hours)
    conn.execute("""INSERT INTO cards(thread_id,channel,ts,strategy,posted_at,scheduled_for,body,actor)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET strategy=excluded.strategy,
        scheduled_for=excluded.scheduled_for, body=excluded.body, cancelled=0,
        actor=excluded.actor""",
        (thread_id, channel, ts, strategy, datetime.now(timezone.utc).isoformat(),
         clock.iso(target), text, actor))
    if override:
        conn.execute("UPDATE cards SET override_at=? WHERE thread_id=?",
                     (datetime.now(timezone.utc).isoformat(), thread_id))
    # Keep the draft as it stood when the click happened. Drafts get regenerated
    # later, so this is the only way to measure how often Sophia edits before
    # sending - the number that decides whether auto-send is safe.
    orig = conn.execute("SELECT body FROM drafts WHERE thread_id=? AND strategy=?",
                        (thread_id, strategy)).fetchone()
    conn.execute("UPDATE cards SET draft_body=? WHERE thread_id=?",
                 (orig["body"] if orig else None, thread_id))
    conn.commit()
    tag = " · DRY RUN, nothing will actually send" if config.DRY_RUN else ""
    by = f" · queued by <@{actor}>" if actor else ""
    client.chat_update(channel=channel, ts=ts, text="Scheduled", blocks=[
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f":clock3: Sending at *{_fmt(target)}*{by}{tag}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:300]}]},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "cancel_send", "style": "danger",
             "text": {"type": "plain_text", "text": "Cancel"},
             "value": json.dumps({"t": thread_id, "s": strategy})}]}])


def run():
    conn = store.connect()
    cfg = config.slack()
    SocketModeHandler(make_app(conn), cfg["xapp"]).start()
