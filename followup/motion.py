"""Where a thread sits in the booking dance.

Derived by regex, not by the model: it's a hard signal from link domains, and a
draft written for the wrong motion is the most damaging output this tool has.
Learned from 251 real threads — all meetings land on daniel@crustdata.co
regardless of which sending persona owns the thread.
"""
import json, re

OUR_LINK   = r"meetings-na2\.hubspot\.com|calendar\.app\.google"
THEIR_LINK = r"calendly\.com|cal\.com/|savvycal|calendar\.app\.google|book\s+a\s+meeting"
PROMISED   = r"send\s+(you\s+)?(an?\s+|the\s+)?(cal(endar)?\s+)?invite|get\s+time\s+on\s+the\s+cal"
# A booking link is only an invitation when the lead actually offered it. Founders
# routinely park "Book 30 minutes with me" in a signature next to their LinkedIn,
# TikTok and Crunchbase links — that is marketing furniture attached to every mail
# they send, not an offer to us. Booking off it presumes an interest they never
# expressed. Two or more profile links in a message means we are reading a
# signature block.
SOCIAL     = (r"linked\s?in|tiktok|youtube|instagram|crunchbase|twitter|\bmy\s+x\b"
              r"|substack|medium\.com|tedx?\s+talk")
# An offer is phrased at us; a signature is not addressed to anyone.
OFFERED    = (r"\b(here('s| is)|use|grab|book|feel\s+free\s+to|you\s+can)"
              r"[^.!?\n]{0,40}\b(my|our|this)\b[^.!?\n]{0,30}"
              r"\b(calendar|cal|link|time|slot|schedule)\b"
              r"|\bmy\s+(calendar|booking)\s+link\b"
              r"|\blink\s+below\b|\buse\s+the\s+link\b"
              r"|\bschedule\s+a\s+time\b|\bpick\s+a\s+time\b|\bgrab\s+(any\s+)?time\b"
              r"|\blet('s| us)\s+(talk|chat|connect|meet|sync|jump\s+on)\b"
              r"|\bhere\s+you\s+go\b")
SIG_TITLE  = (r"\b(ceo|cto|coo|cfo|founder|co-?founder|president|vp|head\s+of|director"
              r"|partner|principal|managing)\b")
# "pay only for meetings booked" is a pricing question, not a confirmation, so
# the bare word is not enough — require a first-person claim or a clear deictic.
BOOKED     = (r"calendly\.com/events/"
              r"|\b(i('ve| have)?\s+(just\s+)?(booked|scheduled|grabbed|put)\b"
              r"|\b(time|slot|meeting|call)\s+(is\s+)?(booked|scheduled)\b"
              r"|\bbooked\s+(you\s+)?(a\s+|some\s+)?(time|slot|meeting|call)\b"
              r"|\bsee\s+you\s+(on|then)\b"
              r"|\blooking\s+forward\s+to\s+(our|the)\s+(call|chat|meeting)\b)")


def _link_is_signature(blob: str, sender: str | None) -> bool:
    """Signature blocks announce themselves: the sender's own address, their job
    title, or a row of profile links sitting within a few lines of the booking
    link. An offer made in prose has none of that around it."""
    lines = [l.strip() for l in blob.splitlines()]
    for i, ln in enumerate(lines):
        if not re.search(THEIR_LINK, ln, re.I):
            continue
        window = "\n".join(lines[max(0, i - 4):i + 3])
        if len({m.group(0).lower() for m in re.finditer(SOCIAL, window, re.I)}) >= 2:
            return True
        if sender and sender.lower() in window.lower():
            return True
        if re.search(SIG_TITLE, window, re.I):
            return True
    return False


GUIDANCE = {
 # The link is already sitting in the thread, usually in our own last message.
 # Sending it again reads as not having noticed they did not use it. Ask whether
 # they managed to book, and give them a reason to: what the call would cover for
 # THEIR company, by name.
 "we_sent_link": "WE ALREADY SENT OUR BOOKING LINK and they have not used it. Do NOT send the "
                 "link again. Ask whether they were able to book a time on the calendar, and "
                 "say what the call would cover for their company by name: \"Hey Konstantine, "
                 "were you able to book a time on my calendar? Would love to chat about how our "
                 "process would work for CYBRI.\" No invite offer, no menu, no link.",
 # Deliberately softer than it looks. A booking link is often just sitting in the
 # lead's email signature rather than being an offer they made, and roughly a
 # third of these threads are exactly that. Claiming they sent us a link, or that
 # we dropped the ball on it, is wrong in a way the lead will notice — so the
 # instruction commits only to the part that is true either way: a link exists and
 # we can use it rather than making them do anything.
 "they_sent_link": "A BOOKING LINK OF THEIRS IS AVAILABLE and nobody has used it. It may be an "
                 "offer they made, or it may just be in their email signature, so do NOT claim "
                 "they sent it to us. Do not apologise, do not ask whether they are still "
                 "interested, do not ask them to do anything. Say we WILL grab a time on their "
                 "calendar - future tense, \"I'll have my colleague, Daniel, grab a time on your "
                 "cal\" (or \"I'll grab a time\" as Daniel) - never past tense: nothing has been "
                 "booked when this is written. One or two sentences.",
 # The link exists but they never offered it, so we have no standing to book. Ask
 # for the interest first; the link is only useful once they say yes.
 "their_link_in_signature": "A BOOKING LINK APPEARS IN THEIR EMAIL SIGNATURE, but they never "
                 "offered it and we have no confirmation they want to meet. Do NOT say we will "
                 "book, do NOT say we will grab a time, and do NOT send them a link. Ask whether "
                 "they had a chance to think it over and say we would love to talk if they are "
                 "interested. One or two sentences.",
 "we_promised_invite": "WE PROMISED TO SEND A CALENDAR INVITE AND NEVER DID. Do not nudge. "
                 "If they asked RECENTLY, just do it: confirm the day or time discussed and say "
                 "the invite is on its way. But if their request is several messages old and they "
                 "have gone quiet since, it has gone stale — do not act on an intent they have not "
                 "confirmed in weeks. Check first and send the invite on their reply: \"Let me "
                 "know if you're still interested, and I'll send over an invite. Would love to "
                 "chat!\" Either way, never nudge and never ask if they got a chance to book.",
 # If a thread reaches drafting as they_booked, it has ALREADY been checked against
 # every host calendar and no meeting was found — the meeting guard holds anything
 # that has one. So this is not "they booked, stay quiet", it is "they think they
 # booked and we cannot see it", which is worth exactly one email.
 # Two cases, split on whether the date they named has passed.
 # Still ahead: one email is worth it, they may have booked into the wrong place.
 # Already gone: a follow-up cannot resolve it — either it happened or it did not,
 # and asking a week later is noise. Hold and let Sophia decide.
 "they_booked": "THEY SAID THEY BOOKED, BUT NOTHING SHOWS ON OUR CALENDAR. If the day they named "
                 "is still ahead, say plainly that you do not have them on the calendar and ask "
                 "them to check it went through: \"I don't have you on my calendar. Do you mind "
                 "checking if the booking went through?\" If the day they named has ALREADY "
                 "PASSED, set needs_followup false with hold_reason naming the date — a follow-up "
                 "cannot resolve a meeting that either happened or did not.",
 # Nobody has mentioned booking yet, so this is decided by the LADDER in the
 # system prompt: what did the lead's last message do?
 "no_booking_talk": "No booking has been discussed yet. Use the ladder: a question or a request "
                 "for material gets answered plus a call suggestion (no link); enthusiasm gets "
                 "the booking link; if we suggested a call and they went quiet without ever "
                 "seeing a calendar, OFFER the cal rather than sending it. Never an invite here.",
 # Two cases, split on whether THEY said they would rebook. Someone who wrote
 # "I'll find another time" gets asked whether they did. Everyone else gets the
 # cancellation acknowledged and the link offered fresh — a cancellation is a new
 # event, so the "we already sent the link" rule does not apply here.
 "meeting_cancelled": "THEIR MEETING WITH US WAS CANCELLED. Do not ask whether they are still "
                 "interested — they already said yes once. Read the fact line 'They have said or "
                 "shown they will rebook'. If YES, ask whether they did and offer the invite as "
                 "the easier route: "
                 "\"Were you able to grab another time? If it's easier let me know a few slots "
                 "and I'll send over a calendar invite.\" OTHERWISE read the fact line 'Who "
                 "cancelled the meeting'. If THEM: open \"Saw our meeting got cancelled, no problem "
                 "at all.\" If UNKNOWN: open \"Sorry our meeting fell through the other week.\" "
                 "(adjust 'the other week' to fit the date: yesterday / last month). Never open with "
                 "'No problem'. Then, either way, both routes: \"Feel free to grab another time that "
                 "works: <booking link> Or if it's easier, let me know a few slots and I can send "
                 "over an invite.\" No guilt, no pressure.",
 # We cancelled a meeting they had ACCEPTED. This is the one place an apology is
 # right - it is ours to own - but briefly, and straight to the reschedule.
 "we_cancelled": "WE CANCELLED A MEETING THEY HAD ACCEPTED. Own it in one short line, no "
                 "explanation, then make rebooking easy with the link: \"Sorry we had to move "
                 "our call. Grab another time that works: <booking link>\" then \"Looking "
                 "forward to chatting!\" Do not ask whether they are still interested, do not "
                 "imply they cancelled, do not offer an invite.",
 # Assume timing, not rejection. Offering an exit invites one: a declined invite is
 # far more often a clashing calendar than a change of heart, and the reply we want
 # is a new time, not permission to stop.
 "meeting_declined": "THEY DECLINED THE CALENDAR INVITE. Treat it as a scheduling clash, not a "
                 "change of heart. Note that it was declined and say it is no problem - but ONLY "
                 "the first time: if the fact line says we already acknowledged the decline, skip "
                 "that line entirely and go straight to \"Were you able to grab another time?\" "
                 "Offer to re-send for a different time. Do NOT offer them the option to drop it and do NOT "
                 "ask whether they are still interested. Name anyone else who was on the invite. "
                 "Two or three sentences.",
 "no_show": "THEY BOOKED AND DID NOT TURN UP. Assume an accident, never a snub — no guilt, no "
                 "mention of being stood up, no apology. One light line and an easy way to rebook.",
}


def classify(email_rows, meeting_state: str | None = None, disposition: str | None = None) -> str:
    """Meeting state outranks anything derived from the email text: what happened
    on the calendar is more recent and more reliable than what was written before
    it."""
    if disposition == "no_show":
        return "no_show"
    if meeting_state == "cancelled":
        return "meeting_cancelled"
    if meeting_state == "we_cancelled":
        return "we_cancelled"
    # invite_withdrawn: they never agreed to a meeting, so the text decides.
    if meeting_state == "declined":
        return "meeting_declined"
    return _classify_text(email_rows)


def _classify_text(email_rows) -> str:
    from .drafting import extract_body, strip_quoted_html
    ours = theirs = promised = booked = sig = False
    for r in email_rows:
        raw = json.loads(r["raw"])
        # Search the message's OWN markup only. The raw html carries the whole
        # quoted chain, so our booking link reappears inside the lead's reply and
        # reads as though they sent us theirs.
        html = strip_quoted_html((raw.get("body") or {}).get("html") or "")
        text = extract_body(raw)
        blob = html + "\n" + text
        if r["is_outbound"]:
            ours = ours or bool(re.search(OUR_LINK, blob, re.I))
            promised = promised or bool(re.search(PROMISED, text, re.I))
        else:
            if re.search(THEIR_LINK, blob, re.I):
                if re.search(OFFERED, text, re.I):
                    theirs = True                 # they pointed us at it
                elif _link_is_signature(blob, r["from_email"]):
                    sig = True                    # signature furniture, not an offer
                else:
                    theirs = True                 # prose link with no signature marks
            booked = booked or bool(re.search(BOOKED, text, re.I))
    if booked:   return "they_booked"
    if theirs:   return "they_sent_link"
    if sig:      return "their_link_in_signature"
    if promised: return "we_promised_invite"
    if ours:     return "we_sent_link"
    return "no_booking_talk"


def for_thread(conn, thread_id: str, email_rows=None) -> str:
    """The classify() every caller should use — looks up meeting state itself so
    no call site can forget to pass it."""
    from . import store
    if email_rows is None:
        email_rows = conn.execute(
            "SELECT * FROM emails WHERE thread_id=? ORDER BY timestamp_email",
            (thread_id,)).fetchall()
    st, m = store.meeting_state(conn, thread_id)
    return classify(email_rows, st, m["disposition"] if m else None)
