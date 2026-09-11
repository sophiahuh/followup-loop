"""Draft generation. Three genuinely different *strategies*, not three rewordings —
the point is that you can choose from a two-word label without reading all three."""
import json, os, pathlib, re, shutil, subprocess
from datetime import datetime, timezone
from . import clock

MODEL = "claude-opus-5"

# new_angle removed. Its premise was that the lead needs convincing, but every
# thread this tool touches is one Instantly already flagged Interested — so it was
# re-pitching people who were past that. Measured over 16 threads: chosen 2 times,
# median 52 words against 25 and 28 for the other two.
STRATEGIES = {
    "soft_nudge": "Soft nudge",
    "direct_ask": "Direct ask",
}

SYSTEM = """You write follow-up emails for a B2B founder-led sales motion. You are \
writing AS the sender, continuing a real thread.

Produce exactly two drafts, one per strategy:
- soft_nudge: brief, low-pressure, references the last thing they said. 2-3 sentences.
- direct_ask: names the ask plainly. Short.
Every lead here has already shown interest, so neither draft should re-explain the offering or \
sell. They are past that.

SHAPE. Every email this team sends opens with a greeting line, "Hey Casey," alone on its own \
line, and then has the same three parts and nothing else:
  1. A one-word or one-line acknowledgement of what they actually said, as the FIRST line of \
     the body, under the greeting. Never in place of the greeting.
     "Will do." / "Absolutely!" / "Awesome!" / "Thanks!" / "Saw the invite got declined, no problem at all."
  2. ONE action, stated as a fact. Not offered, not proposed as an option.
     "I had my colleague, Daniel, send an invite for that week."
     "I had my colleague, Daniel, book a time on your calendar."
     "Grab any time that works:" followed by the ACTUAL booking URL copied from earlier in
     the thread. Never write a placeholder like <link> or [link] — whatever you write is sent
     verbatim, so a placeholder ships as a placeholder.
  3. One short closing line. "Let me know if it works!" / "Looking forward to meeting!"
Sixteen to thirty words total. If your draft is longer, you have added something that does not
belong in it.

ONE EXCEPTION TO COMMITTING. If the lead has raised a constraint we cannot resolve on our own —
a timezone, a window they need, a person who has to be there, a date they are away — then the
single right action is to ASK about that constraint, not to book around it. Booking blindly just
produces another decline. Keep it to one question:
  "Absolutely, what window works for you in SGT?"
Do not book AND ask. Ask.

ONE ACTION, WITH ONE EXCEPTION.
If the lead HAS told you what they want, do exactly that and mention nothing else. They asked for
an invite: send the invite, never mention the link. They asked for the link: give the link, never
mention an invite. Offering both here is the most common way these drafts go wrong.
If the lead has NOT said which they prefer AND the route we already offered has visibly failed —
a booking that was cancelled or declined, a link they have ignored more than once — then offer the
other route alongside it, because repeating the thing that did not work is worse:
  "Grab another time that works, or if it's easier let me know a few slots and I'll send an
  invite." (again, the real URL, never a placeholder)

Rules:
- Match the sender's voice from earlier messages in the thread. Lowercase-casual is fine \
if that's how they write.
- No "just circling back", "wanted to check in" or similar filler openers. "Bumping this," and \
"Hope you're well." are fine — this team uses both. Never OPEN with "No problem" - it may follow \
an acknowledgement ("Saw our meeting got cancelled, no problem at all.") but never lead. Never "Thanks again" and never re-thank them \
for a message we already thanked them for: a follow-up opens with the follow-up.
- Never invent facts about the lead's company, funding, headcount, or product.
- Plain text, no markdown, no signature block, no subject line.
- The only URLs you may use are the two given below, or one copied verbatim from earlier in this
thread. Use the booking link when they need to book. Use the website ONLY if they have asked for
more information — never volunteer it, since everyone here is already interested. Never invent one and never leave a placeholder like <link> or [link] —
the draft is sent exactly as written.
- Address whoever actually wrote last, not the name on the lead record. If a colleague was looped
in and is now the one replying, you are writing to them. Use the first name they sign off with.
- Do NOT apologise for the delay, do not say sorry for the slow reply, and do not reference how \
long it has been. The tool exists so follow-ups are prompt; an apology reads as an admission of a \
problem that will not exist. Just write the next message.
- Asking "are you still interested?" is normally forbidden — but it is RIGHT in exactly one case:
the lead has not replied to our last TWO messages IN A ROW - see the fact line "Consecutive \
messages from us with no reply". One is not silence. Two is, and
a check is fairer than a third nudge. ONLY when that fact line says the check is allowed, and \
only then, use this team's wording. If they HAVE been sent the link before: "If you're still \
interested, would love to get a call on the calendar. Feel free to send over some times that \
work for you if it's easier. Let me know!" If they have NEVER been sent a calendar (rung 1b): \
"Let me know if you're still interested, and I can send over my cal. Would love to chat!" \
No link, no invite, under 30 words, and open with "Hope you're well." if it has been a while. \
When the fact line says do NOT ask, this wording is forbidden - use the ladder rung instead.
- THE LADDER. Read the lead's last message and pick ONE rung. Measured on 199 real emails from \
this team: the link appears in 60%, a call is suggested without a link in 15%, an invite in 1%.
  1. They asked a QUESTION or for MATERIAL ("do you have a deck?", "do you charge per \
     appointment?", "what about restaurants?"): answer it, then suggest a call to walk through how \
     it would work for their company, by name. NO link. "Would love to get on a quick call to walk \
     through how this could work for SumZero."
  1b. We suggested a call, they went quiet, and they have NEVER been sent a calendar: OFFER the \
     cal, do not send it. "Let me know if you're still interested, and I can send over my cal. \
     Would love to chat!" Sending a link to someone who never asked for one reads as pushy.
  2. They showed ENTHUSIASM or openness without asking for a specific thing ("happy to chat", \
     "I'd love to hear more", "open to learning more", "interested in a discussion", "sure, let me \
     know how this works"). Wanting to hear more is NOT a request for material — it is rung 2, \
     and the answer is the link. Rung 1 needs a concrete ask: a deck, a price, a case study, a \
     capability question. Asking us to use a different email address, cc someone, or write to \
     a colleague is ROUTING, not a request — honour it in passing and it does not change the \
     rung. When in doubt between 1 and 2, it is 2. Send the \
     booking link, warmly, in this team's words: "Grab any time that works for you on my cal:" / \
     "Feel free to grab any time that works on my calendar:". Do not pitch, do not add context.
  3. They GAVE TIMES or said "send me an invite": confirm and send the invite. This is the ONLY \
     rung where the word invite appears. "Wednesday at 1:30pm PT works great. I had my colleague, \
     Daniel, send over a calendar invite." BUT check the fact line "Days since their last \
     message": if the times they offered are more than 7 days old, that window is GONE. Never \
     confirm it and never claim an invite was sent for it. Ask which day works now and commit to \
     the invite on their answer: "Does Wednesday or Thursday at 9am ET still work for you? Let me \
     know and I'll have my colleague, Daniel, send over an invite."
  4. We already sent the link and they went quiet: ask whether they were able to book. No link \
     again, no invite. THIS RUNG WINS over rung 3: if they offered times and we answered with the \
     link instead of confirming one, the link superseded their times. Do not go back and confirm \
     a time now, and do not claim an invite was sent — none was. "Were you able to book a time? \
     Looking forward to chatting!" One unanswered link is rung 4, NOT the "still interested" \
     check — that needs two unanswered messages from us. EXCEPTION: a meeting that was CANCELLED resets this — the \
     booking state guidance for a cancellation decides, and it may send the link again.
  If the lead has ALREADY said yes, never pitch or re-explain the offering. They are past that.
- If the lead's last message ASKED US TO DO SOMETHING and we never answered it, doing that thing IS the follow-up. Answer the request itself: do not nudge around it, and do not offer a menu of alternatives they did not ask for. Match the FORM they asked for — someone who asked for a calendar invite, or who told us the booking link did not show the dates they need, wants an invite, not the link again. If the window they named has since passed, commit to the same thing for a fresh one rather than quietly dropping it. This overrides the booking state below.
- If OUR most recent message already contains the booking link, the follow-up must NOT contain \
a link. They have it. Ask whether they were able to book a time, and name what the call would \
cover for their company. Repeating a link they just ignored reads as not having noticed.
- Never invent a specific time or date to propose. You do not have access to anyone's calendar and
cannot know what is free. Re-send the booking link, or say an invite is coming, and let them pick.
- SHAPE: the greeting on its own line, then a blank line, then the body, then a blank line, \
then the sign-off on its own line with the name on the line below. "Hey Nikhil,\\n\\nBumping this, \
did you get a chance to take a look?\\n\\nBest,\\nDaniel". Never run the greeting into the first \
sentence.
- Prefer a comma to a dash. Only 14 of 199 real emails from this team contain a dash of any kind.
- The EXAMPLES below are real follow-ups this team has actually sent. Match their length, their
register and their habits far more closely than any instruction here. If the examples are shorter
and blunter than you would naturally write, they are right and you are wrong.
- Obey the booking state above. It is derived from the actual links in the thread and it \
overrides your own reading.
- Meetings are hosted by Daniel whichever persona owns the thread. If you are writing as anyone \
OTHER than Daniel or Dan, this team says it one way and you should copy it exactly: "I had my \
colleague, Daniel, send over a calendar invite." / "Just had my colleague, Daniel, book a meeting \
for Monday at 3:45." If you ARE Daniel or Dan, never refer to yourself in the third person — it is \
simply "I sent an invite". Never give his surname or email address, and never write that something \
"will come from" him.
- The word INVITE, and any mention of Daniel sending one, is ONLY for rung 3: the lead said \
"send me an invite" or gave specific times. Nowhere else — not as an offer, not because they seem \
keen. This team wrote "invite" 3 times in 199 emails and two of those were confirming a time the \
lead had named. ONE EDGE CASE: a meeting that was cancelled or declined. There the invite is \
ALWAYS offered as the easier route alongside the link, because the booking route visibly failed: \
"...or if it's easier, let me know a few slots and I can send over an invite." Follow the booking \
state guidance for the exact wording.

BEFORE ANYTHING ELSE, AND INDEPENDENTLY OF THE BOOKING STATE BELOW — if writing a good reply here
needs knowledge you do not have, set \
needs_human true and say what is missing. That means: how we compare to a named competitor, what \
Crustdata can or cannot technically do, pricing beyond what appears in the thread, contractual \
terms, named customer references, or any commitment about timelines or deliverables. Do NOT guess \
and do NOT write around it — a confident wrong claim about our own product is worse than no draft. \
Still fill in drafts as a starting point, but keep them to the parts you can actually stand behind.
This check is NOT cancelled by anything that happened on the calendar. If the lead asked a
commercial or product question that was never answered, it stays unanswered whether or not a
meeting was later booked, cancelled or declined — scan the WHOLE thread for an open question of
that kind, not just the most recent message.
Also set needs_human when scheduling now runs through an assistant or a third party the lead has
named ("Rachel will grab time"), since coordinating with them is not something you can do from
this thread.

THEN decide whether a follow-up is warranted at all. Set needs_followup to false if the \
thread shows the lead is waiting on something from us that we have not sent, has explicitly \
asked us to follow up at a later date that has not arrived, or has declined. A lead who SAYS they \
booked is NOT a reason to hold — the calendar has already been checked and found nothing, so ask \
them to verify it went through. Nudging someone who just booked a meeting actively damages the deal.

Return ONLY valid JSON:
{"needs_followup": true|false, "hold_reason": "<12 words max, only when false>",
 "needs_human": true|false, "human_reason": "<what you would need to answer this, 15 words max>",
 "recommended": "<strategy key>", "why": "<8 words max>",
 "drafts": {"soft_nudge": "...", "direct_ask": "..."}}
When needs_followup is false, still fill drafts — they stay available behind a button in case \
the judgement is wrong."""


def api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    p = pathlib.Path.home() / ".anthropic_key"
    if not p.exists():
        return None
    # tolerate a stray leading '=' or quotes from a shell paste
    return p.read_text().strip().lstrip('="\'').strip() or None


def backend() -> str:
    """Prefer a real API key; fall back to the local `claude` CLI, which uses the
    existing subscription and needs no key or separate billing."""
    if api_key():
        return "api"
    if shutil.which("claude"):
        return "cli"
    raise SystemExit("no ~/.anthropic_key and no `claude` on PATH")


QUOTE_CUTS = [
    r"\bOn\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[^\n]{0,90}?\bwrote:",   # Gmail / Apple
    r"\bOn\s+\w{3,9}\s+\d{1,2},?\s+\d{4}[^\n]{0,90}?\bwrote:",
    r"\bOn\s+\d{1,2}\s+\w{3,9}\s+\d{4}[^\n]{0,90}?\bwrote:",
    r"-{2,}\s*Original Message\s*-{2,}",
    r"\bFrom:\s*.{0,120}?\b(Sent|Date):",                          # Outlook header block
    r"_{10,}",                                                       # Outlook separator
    r"\bGet Outlook for \w+",
    r"\n\s*>",                                                      # raw quoting
]


def strip_quoted_html(src: str) -> str:
    """Drop the quoted reply chain from raw HTML, keeping the rest of the markup.

    Used both by extract_body (before tags are stripped) and by motion
    classification, which must search the HTML directly — a booking link often
    lives only in an anchor's href, so classifying on plain text would lose it,
    while classifying on the raw HTML sees OUR link quoted inside THEIR reply and
    concludes they sent us theirs. Measured: 13 of 29 threads misread that way.
    """
    src = re.sub(r"(?is)<(script|style).*?</\1>", " ", src)
    src = re.sub(r"(?is)<blockquote.*", " ", src)
    src = re.sub(r'(?is)<div[^>]*class="[^"]*gmail_quote.*', " ", src)
    src = re.sub(r'(?is)<div[^>]*id="(appendonsend|divRplyFwdMsg)".*', " ", src)
    src = re.sub(r'(?is)<hr[^>]*id="?stopSpelling.*', " ", src)
    # plain-text style quoting that survives inside an HTML body
    src = re.split(r"(?i)<br[^>]*>\s*On .{0,80}?wrote:", src)[0]
    src = re.split(r"(?i)-{2,}\s*Original Message\s*-{2,}", src)[0]
    return src


def strip_quotes(text: str) -> str:
    """Cut everything from the first quoted-reply marker onward.

    Without this, each email carries the whole prior thread, so Slack's "Show
    more" expands one message into the entire conversation.
    """
    cut = len(text)
    for pat in QUOTE_CUTS:
        m = re.search(pat, text, re.I)
        if m:
            cut = min(cut, m.start())
    out = text[:cut]
    # standard signature delimiter — a line of nothing but dashes
    m = re.search(r"\n\s*-{2,}\s*\n", out)
    if m and m.start() > len(out) * 0.4:
        out = out[:m.start()]
    # a leading "Re: <subject>" repeats what the card already says
    out = re.sub(r"^(Re|Fwd|RE|FW)\s*:.*?(\n|$)", "", out.strip(), count=1)
    # the HTML conversion leaves a stray space at the head of most lines
    lines = [ln.strip() for ln in out.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_body(raw: dict) -> str:
    """Campaign step-1 emails carry HTML only — no text, no preview. Reading
    body.text alone silently drops the original pitch, which is the context a
    follow-up most depends on."""
    b = raw.get("body") or {}
    src = b.get("text") or ""
    if not src.strip():
        src = b.get("html") or raw.get("content_preview") or ""
    src = strip_quoted_html(src)
    src = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", src)
    src = re.sub(r"<[^>]+>", " ", src)
    for a, b_ in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"),
                  ("&quot;", '"')):
        src = src.replace(a, b_)
    src = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", src)).strip()
    return strip_quotes(src)


def render_thread(rows) -> str:
    out = []
    for r in rows:
        # Name the inbound sender. Colleagues get looped in constantly — the lead
        # record says chinmay@ while Kal is the one actually corresponding — and a
        # draft addressed to the wrong person is immediately obvious to them.
        who = "US" if r["is_outbound"] else f"THEM <{r['from_email']}>"
        body = extract_body(json.loads(r["raw"]))[:1500]
        out.append(f"[{who} · {r['timestamp_email'][:10]}] {body}")
    return "\n\n".join(out)


def sender_name(email_rows) -> str:
    """Which persona owns this thread. Taken from the sending account rather than
    guessed from sign-offs, because the answer changes what the draft may say
    about itself: Daniel writing as Daniel says "I", anyone else says "my
    colleague, Daniel"."""
    out = [r for r in email_rows if r["is_outbound"] and (r["eaccount"] or r["from_email"])]
    if not out:
        return ""
    local = (out[-1]["eaccount"] or out[-1]["from_email"]).split("@")[0]
    first = re.split(r"[._-]", local)[0]
    return first[:1].upper() + first[1:].lower() if first else ""


def build_prompt(thread_row, email_rows, conn=None) -> str:
    from .motion import classify, for_thread, GUIDANCE
    motion = (for_thread(conn, thread_row["thread_id"], email_rows) if conn
              else classify(email_rows))
    anchor = clock.parse(thread_row["clock_start_at"])
    age = (datetime.now(timezone.utc) - anchor).days if anchor else 0
    replied = "yes" if thread_row["last_inbound_at"] else "never replied"
    from .examples import retrieve
    last_in = next((r for r in reversed(list(email_rows)) if not r["is_outbound"]), None)
    ctx = extract_body(json.loads(last_in["raw"])) if last_in is not None else ""
    _trail = 0
    for _r in reversed(list(email_rows)):
        if _r["is_outbound"]: _trail += 1
        else: break
    ex = retrieve(conn, motion, ctx, allow_still_interested=_trail >= 2) if conn is not None else []
    ex_block = ("\n--- HOW THIS TEAM ACTUALLY WRITES (real sent follow-ups) ---\n"
                + "\n\n---\n".join(e.strip() for e in ex) + "\n--- END EXAMPLES ---\n\n") if ex else ""
    from . import config
    from .motion import OUR_LINK
    who = sender_name(email_rows)
    # Stated as a fact rather than left for the model to notice: whether the
    # link is already sitting in our own last message decides whether the
    # follow-up may carry one at all.
    last_out = next((r for r in reversed(list(email_rows)) if r["is_outbound"]), None)
    # The message's OWN content only. The raw html carries the whole quoted
    # chain, so a link from three messages ago would otherwise count as "in our
    # last message" — which is exactly what happened on Jake's thread.
    # extract_body alone. strip_quoted_html misses some quote wrappers and let
    # the original campaign email (with its link) leak through on Jake's thread;
    # extract_body returned exactly the 59 characters Dan actually typed.
    link_in_last = bool(last_out is not None and re.search(
        OUR_LINK, extract_body(json.loads(last_out["raw"])), re.I))
    # After a cancellation, whether THEY have said or shown they will rebook
    # decides the draft, and it is a fact, not a judgement: either they have
    # booked more than once, or they wrote "I'll find another time" after a
    # booking fell through. "Rachel will grab time" written before any meeting
    # existed is how the meeting got booked, not a promise to rebook.
    # Trailing run of our own messages with nothing from them in between. The
    # "still interested" check needs two; the model was counting every message
    # we ever sent instead.
    unanswered = 0
    for r in reversed(list(email_rows)):
        if r["is_outbound"]:
            unanswered += 1
        else:
            break
    # Who cancelled, as far as the calendar can tell. Only the organizer can
    # cancel a Google event: if the LEAD organised it (their own invite), it was
    # them; a booking made through HubSpot/Calendly can be cancelled from either
    # side, so the side is unknown. The opener depends on this.
    cancel_side = "n/a"
    if conn is not None and thread_row["meeting_state"] == "cancelled":
        mr = conn.execute("""SELECT organizer, booking_tool FROM meetings
            WHERE (thread_id=? OR lead=?) AND status='cancelled'
            ORDER BY start_at DESC LIMIT 1""", (thread_row["thread_id"], thread_row["lead"])).fetchone()
        if mr:
            org, tool = (mr["organizer"] or "").lower(), (mr["booking_tool"] or "manual")
            cancel_side = ("THEM - they organised the invite themselves"
                           if tool == "manual" and "crustdata" not in org and org
                           else "UNKNOWN - booked via a tool, either side could have cancelled")
    rebook = False
    if conn is not None:
        mrows = conn.execute("""SELECT start_at, status FROM meetings
            WHERE thread_id=? OR (? IS NOT NULL AND lead=?)""",
            (thread_row["thread_id"], thread_row["lead"], thread_row["lead"])).fetchall()
        cancelled_at = [m["start_at"] for m in mrows if m["status"] == "cancelled"]
        if len(mrows) >= 2:
            rebook = True
        elif cancelled_at:
            first_cancel = min(cancelled_at)
            for r in email_rows:
                if not r["is_outbound"] and r["timestamp_email"] > first_cancel and re.search(
                        r"(find|grab|book|pick|look for)\s+(another|a new|a different)\s+(time|slot)"
                        r"|re-?book|re-?schedule", extract_body(json.loads(r["raw"])), re.I):
                    rebook = True
    return (ex_block +
            (f"You are writing as {who}. Sign off as {who}.\n" if who else "") +
            f"Booking link: {config.BOOKING_LINK}\n"
            f"Website, ONLY if they asked for more information: {config.WEBSITE_LINK}\n" +
            f"Lead: {thread_row['lead']}\n"
            f"Days since our last email: {age}\n"
            f"Has the lead ever replied: {replied}\n"
            f"Booking state: {motion}\n"
            f"Our last message already contains the booking link: "
            f"{'no - a cancellation resets this, the link may be offered again' if motion in ('meeting_cancelled', 'we_cancelled') else ('YES, do not send it again' if link_in_last else 'no')}\n"
            f"Days since their last message: {(datetime.now(timezone.utc) - clock.parse(last_in['timestamp_email'])).days if last_in is not None else 'n/a'}\n"
            f"Who cancelled the meeting: {cancel_side}\n"
            f"They have said or shown they will rebook after a cancellation: "
            f"{'YES - ask whether they managed to, offer the invite as the easier route' if rebook else 'NO - do not ask whether they booked; acknowledge the cancellation and offer the booking link'}\n"
            f"Consecutive messages from us with no reply: {unanswered}"
            f"{' - the still-interested check is allowed' if unanswered >= 2 else ' - do NOT ask if they are still interested'}\n"
            f"WHAT THIS FOLLOW-UP MUST DO: {GUIDANCE[motion]}\n\n"
            f"--- THREAD ---\n{render_thread(email_rows)}\n--- END ---\n\n"
            "Write the two drafts.")


def _create_with_retry(client, **kw):
    """Anthropic returns 529 Overloaded under load; one draft failing is fine
    (the thread comes back next cycle) but a burst of forty back-to-back calls
    during a redraft should not fall over on the first one. Three tries, backing
    off, and only for the transient statuses."""
    import time, anthropic
    for attempt in range(5):
        try:
            return client.messages.create(**kw)
        except anthropic.APIStatusError as e:
            if e.status_code not in (429, 500, 502, 503, 529) or attempt == 4:
                raise
            time.sleep(5 * 2 ** attempt)          # 5, 10, 20, 40s


# Sophia's emails have one shape and she almost never strays from it:
#
#     Hey Nikhil,
#
#     Bumping this - did you get a chance to take a look?
#
#     Best,
#     Daniel
#
# The model gets told, but an instruction is approximate and a shape is not.
# This forces every draft into it after the fact: greeting on its own line, a
# blank line, the body, a blank line, the sign-off on its own two lines.
_GREETING = re.compile(r"^\s*((?:hey|hi|hello|dear)\s+[^,\n]{1,40},)\s*", re.I)
_SIGNOFF  = re.compile(r"\s*((?i:best|thanks|thank you|cheers|best regards|kind regards|regards|talk soon),?)"
                       r"\s*\n?\s*([A-Z][\w .'-]{0,40})\s*$")


def normalize(text: str, sender: str = "") -> str:
    """`sender` is the persona that owns the thread; used only to add the
    sign-off when the model left it off, never to change one it wrote."""
    if not text or text.lstrip().startswith("_["):      # test placeholders
        return text
    greeting = signoff = ""
    m = _GREETING.match(text)
    if m:
        greeting, text = m.group(1), text[m.end():]
    m = _SIGNOFF.search(text)
    if m:
        signoff, text = f"{m.group(1)}\n{m.group(2)}", text[:m.start()]
    body = re.sub(r"[ \t]+\n", "\n", text.strip())          # trailing spaces
    body = re.sub(r"\n{3,}", "\n\n", body)                   # at most one blank line
    # "Bumping this, were you able to book?" says the same thing twice. Sophia
    # cut it by hand four times today: when the nudge is followed by a question,
    # the question alone is the nudge.
    body = re.sub(r"^\s*bumping this[,\-\s]+(?=(were|did|have|has|any|would|is|are|what|which|when|could|can|do|does)\b)",
                  "", body, flags=re.I)
    # "Hope you're well. Bumping this, ..." is two openers. Keep the first.
    body = re.sub(r"^(hope you'?re well[.!]\s*)bumping this[,\-\s]+([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), body, flags=re.I)
    body = body[:1].upper() + body[1:] if body else body
    if body:
        body = body[0].upper() + body[1:]
    if not signoff and sender:
        signoff = f"Best,\n{sender}"
    parts = [x for x in (greeting, body, signoff) if x]
    return "\n\n".join(parts)


def generate(thread_row, email_rows, conn=None) -> dict:
    prompt = build_prompt(thread_row, email_rows, conn)
    if backend() == "api":
        import anthropic
        msg = _create_with_retry(anthropic.Anthropic(api_key=api_key()),
            model=MODEL, max_tokens=8000, system=SYSTEM,
            # Opus 5 runs adaptive thinking by default; medium effort is ample for
            # a short email and keeps the per-draft cost down.
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}])
        # thinking blocks come first — take the text blocks, not content[0]
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
    else:
        r = subprocess.run(["claude", "-p", f"{SYSTEM}\n\n{prompt}"],
                           capture_output=True, text=True, timeout=240)
        if r.returncode != 0:
            raise RuntimeError(f"claude cli failed: {r.stderr[:300]}")
        text = r.stdout.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    data = json.loads(text)
    if data.get("recommended") not in STRATEGIES:
        data["recommended"] = "soft_nudge"
    data["needs_followup"] = bool(data.get("needs_followup", True))
    data["needs_human"] = bool(data.get("needs_human", False))
    who = sender_name(email_rows)
    data["drafts"] = {k: normalize(v, who) for k, v in (data.get("drafts") or {}).items()}
    return data
