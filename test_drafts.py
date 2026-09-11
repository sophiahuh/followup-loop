#!/usr/bin/env python3
"""Regression suite for draft quality.

Every assertion here comes from a real correction Sophia made against a real
thread. Rules interact — silence beats promised-invite, a raised constraint beats
commit-to-one-action, a failed route beats never-offer-a-menu — and a new rule can
silently disable an older one. This is what catches that.

    python3 test_drafts.py            # all fixtures
    python3 test_drafts.py kris       # one, by substring
"""
import json, re, sys, statistics
from followup import store, drafting, motion, slack_app, config

def has_link(t):    return bool(re.search(r"https?://", t))
def has_dash(t):    return "—" in t or " - " in t
def has_time(t):    return bool(re.search(r"\bat \d{1,2}(:\d{2})?\s?(am|pm)\b", t, re.I))
def apologises(t):  return bool(re.search(r"apolog|sorry for the|slow reply|long gap|my bad", t, re.I))
def surname(t):     return "Ahmadizadeh" in t
def still_int(t):   return bool(re.search(r"still (interested|keen|up for)", t, re.I))
def pitches(t):
    # The website link is legitimate when they asked for information, so the check
    # is on pitch LANGUAGE only, not on the URL.
    return bool(re.search(r"600k|\$10M ARR|xAI|Deel|Decagon|2x YC|A16Z", t, re.I))
def menu(t):        return bool(re.search(r"\bor\b.{0,60}(invite|slot|time|link)", t, re.I))
def books(t):
    # A CLAIM that we are taking a slot on their calendar - not merely proposing
    # a call. "I'll grab a time" is a claim; "would love to get a call set up" is
    # an offer, and the earlier version failed that distinction.
    return bool(re.search(r"\b(i'?ll|i will|i'?ve|i have|we'?ll|we will)\b[^.!?]{0,40}"
                          r"\b(grab|book|put|hold|set\s?up|schedul\w+)\b"
                          r"|on your calendar|colleague,\s+Daniel,?\s+(book|grab|put|send)",
                          t, re.I))

def shaped(t):
    """Sophia's shape: greeting alone on line 1, blank line 2, and if there is a
    sign-off it sits on its own line after a blank, name on the line below."""
    lines = t.split("\n")
    if len(lines) < 3: return False
    if not re.match(r"^(hey|hi|hello)\s+[^,]{1,40},$", lines[0].strip(), re.I): return False
    if lines[1].strip(): return False
    m = re.search(r"\n\n(best|thanks|thank you|cheers|regards)[,]?\n[A-Z][\w .'-]{0,40}\s*$", t, re.I)
    return bool(m)

# Applied to every draft of every fixture — these are the unconditional rules.
GLOBAL = [
    ("shaped like her emails",   lambda t, f: shaped(t)),
    ("no em-dash",               lambda t, f: not has_dash(t)),
    # A clock time is invented unless the lead wrote it first - "Wednesday at 9am ET"
    # quoted back to Raj is his, not ours.
    ("no invented time",         lambda t, f: not has_time(t) or all(
        m.group(1).lower().replace(" ", "") in f.get("thread_text", "")
        for m in re.finditer(r"\bat (\d{1,2}(?::\d{2})?\s?(?:am|pm))\b", t, re.I))),
    ("no surname",               lambda t, f: not surname(t)),
    ("no delay apology",         lambda t, f: not apologises(t)),
    ("no re-pitching",           lambda t, f: not pitches(t)),
    ("no placeholder url",       lambda t, f: not re.search(r"[<\[](link|url|calendar[ _-]?link)[>\]]", t, re.I)),
    ("only sanctioned urls",     lambda t, f: all(
        u.rstrip(".,)") in (config.BOOKING_LINK, config.WEBSITE_LINK) or u in f["thread_urls"]
        for u in re.findall(r"https?://[^\s<>\"\')]+", t))),
    # 45, not 40: Sophia's own cancellation wording (fell through + link + invite
    # route) runs ~38 words before the greeting and sign-off are added.
    ("under 45 words",           lambda t, f: len(t.split()) <= 45),
    ("first person when Daniel", lambda t, f: not (f["as"] in ("Daniel", "Dan")
                                                   and "colleague, Daniel" in t)),
]

FIXTURES = [
    {"lead": "kris@getsynopsis.ai",
     "ideal": "Will do. I had my colleague, Daniel, send an invite for that week. Let me know if it works!",
     "why": "he named the week he wants, so commit to the invite and drop the link",
     "must": [("offers an invite", lambda t: "invite" in t.lower())],
     "must_not": [("sends a link", has_link), ("asks if still interested", still_int)]},

    {"lead": "wangwarren430@gmail.com",
     "ideal": "Absolutely! I'd love to tell you more over a quick call. Grab any time that works: [link]",
     "why": "interested and just wants the link; also asked us to use founders@rebulk.com",
     "must": [("gives the link", has_link)],
     "must_not": [("asks if still interested", still_int)],
     "card": [("flags the redirect address",
               lambda conn, t: slack_app.redirect_address(conn, t) == "founders@rebulk.com")]},

    {"lead": "mark.naufel@gmail.com",
     "ideal": "Let me know if you're still interested, and I'll send over an invite. Would love to chat!",
     "why": "asked for an invite a month ago then went silent through two of our messages",
     "must": [("checks interest first", still_int)],
     "must_not": [("sends a link", has_link)]},

    {"lead": "ejordan@younifiedai.com",
     "ideal": "Did you get a chance to think this over? Would love to chat more if you're "
              "interested and walk through how this could work for YOUnifiedAI.",
     "why": "her booking link is only in her signature, so there is no interest to book against",
     "expect": {"needs_human": True},
     "always": [("must not claim we will book", lambda t: not books(t)),
                ("must not send a link", lambda t: not has_link(t))]},

    {"lead": "divya@sumzero.com",
     "ideal": "Let me know if you're still interested, and I can send over my cal. Would love to chat!",
     "why": "asked for info, never showed interest in a call, never got a cal, so offer it rather than send it",
     "must": [("checks interest first", still_int),
              ("offers the cal", lambda t: bool(re.search(r"\b(send|share)\b[^.!?]{0,30}\b(cal|calendar|link)\b", t, re.I)))],
     "must_not": [("sends a link", has_link), ("says invite", lambda t: "invite" in t.lower())]},

    {"lead": "yasirdrabu@gmail.com",
     "ideal": "If you're still interested, would love to get a call on the calendar. Feel free to send "
              "over some times that work for you if it's easier. Let me know!",
     "why": "link sent, then one bump, both unanswered: two in a row means the still-interested check",
     "must": [("checks interest", still_int),
              ("offers to take their times", lambda t: bool(re.search(r"send (over|me)\s+(some|a few)?\s*(times|slots)", t, re.I)))],
     "must_not": [("sends a link", has_link), ("says invite", lambda t: "invite" in t.lower()),
                  ("confirms a clock time", lambda t: bool(re.search(r"\b\d{1,2}\s?(am|pm)\b", t, re.I)))]},

    # Sophia sent the "were you able to book" bump on 3 Sep. With the link and
    # that bump both unanswered, this thread is now the second follow-up.
    # Sarah sent him a calendar invite he never accepted, then withdrew it. From
    # his side no meeting was ever agreed, so the draft must not mention a
    # cancellation - he asked for materials, got a call suggestion, went quiet.
    {"lead": "calvin@flowbotforge.com",
     "ideal": "Did you get a chance to look at the site? Let me know if you're still interested, and I "
              "can send over my cal. Would love to chat!",
     "why": "an invite we sent and withdrew is not a cancellation from his side: plain ladder, rung 1b",
     "must_not": [("mentions a cancellation", lambda t: bool(re.search(r"cancel|reschedul|move our call", t, re.I))),
                  ("sends a link", has_link), ("says invite", lambda t: "invite" in t.lower())]},

    # Booked through our HubSpot link, cancelled an hour before, never said he'd
    # rebook. We cannot tell which side cancelled, so: "fell through", both routes.
    {"lead": "dan@usemassive.com",
     "ideal": "Sorry last week's meeting fell through. Feel free to grab another time that works: "
              "[link] Or if it's easier, let me know a few slots and I can send over an invite.",
     "why": "uncertain who cancelled a HubSpot booking: neutral wording, link plus invite as the easier route",
     "must": [("says it fell through", lambda t: bool(re.search(r"fell through", t, re.I))),
              ("offers the link", has_link),
              ("offers the invite as the easier route", lambda t: "invite" in t.lower())],
     "must_not": [("blames them", lambda t: bool(re.search(r"(you|they) cancelled|got cancelled|saw .*cancel", t, re.I))),
                  ("opens with No problem", lambda t: bool(re.search(r"^\s*(hey|hi)[^\n]*\n+\s*no problem", t, re.I))),
                  ("asks if still interested", still_int)]},

    # Sophia sent Raj the link herself on Sept 3 ("Feel free to grab a time...").
    # His Aug 8 offer of times is superseded: this is now rung 4 inside two weeks.
    {"lead": "raj@kronosx.ai",
     "ideal": "Were you able to grab a time on my calendar? Would love to walk through how our process would work for KronosX.",
     "why": "link sent 3 Sep and unanswered: ask if he grabbed a time, no link yet, no invite claim",
     "must": [("asks if they grabbed a time", lambda t: bool(re.search(r"able to (grab|book)|did you get a chance to (grab|book)", t, re.I)))],
     "must_not": [("re-sends the link inside two weeks", has_link),
                  ("claims an invite was sent", lambda t: bool(re.search(r"\bhad my colleague, Daniel, (send|book)|\bsent (over )?(an|the|a) (calendar )?invite", t, re.I))),
                  ("bumping plus a question", lambda t: bool(re.search(r"bumping this[,\-\s]+(were you able|did you get)", t, re.I)))]},

    # Eric replied twice, then went quiet after our link 23 days ago. One unanswered
    # message, link over two weeks old: the link comes back, softly.
    {"lead": "eric@curbsidehealth.com",
     "ideal": "Hope you're well. Feel free to grab a time that works: [link] Looking forward to chatting!",
     "why": "link last seen 23 days ago and one unanswered: buried, so re-send it with soft framing",
     "must": [("re-sends the link", has_link),
              ("soft framing", lambda t: bool(re.search(r"feel free to grab", t, re.I)))],
     "must_not": [("first-link energy", lambda t: bool(re.search(r"grab any time", t, re.I))),
                  ("asks if still interested", still_int),
                  ("bumping plus a question", lambda t: bool(re.search(r"bumping this[,\-\s]+(were you able|did you get)", t, re.I)))]},
    # No lead is currently in the "decline acknowledged once, one unanswered" or
    # "they sent their link and wrote last" state (Sophia cleared them today), so
    # those two rules are encoded in the prompt but carry no fixture until a real
    # case appears. Do not invent one from a thread that has moved on.

    {"lead": "konstantine@cybri.com",
     "ideal": "If you're still interested, would love to get a call on the calendar. Feel free to send "
              "over some times that work for you if it's easier. Let me know!",
     "why": "link sent, then one bump, both unanswered: two in a row means the still-interested check",
     "must": [("checks interest", still_int),
              ("offers to take their times", lambda t: bool(re.search(r"send (over|me)\s+(some|a few)?\s*(times|slots)", t, re.I)))],
     "must_not": [("sends a link", has_link), ("offers an invite", lambda t: "invite" in t.lower()),
                  ("re-thanks them", lambda t: bool(re.search(r"thanks again|thank you again", t, re.I)))]},

    # The organizer field settled what actually happened here: she DECLINED the
    # Sept 3 invite, and the "cancellation" was us deleting the dead event. So
    # the declined rule applies (Aakash, Suchit): note the decline as a clash,
    # offer to re-send. Sophia's earlier cancellation-wording ideal was written
    # on the false premise; revisit if she prefers the link version for declines.
    {"lead": "sarah@hydrondesal.com",
     "why": "free-trial question never answered, and Rachel schedules for her; she declined the invite",
     "expect": {"needs_human": True},
     "always": [("treats it as a decline, not their cancellation",
                 lambda t: not re.search(r"(you|they) cancelled|got cancelled", t, re.I)),
                ("does not ask if still interested", lambda t: not still_int(t))]},

    {"lead": "neliniav@gmail.com",
     "why": "her Aug 27 demo is on Daniel's calendar under nelinia@strategicvalueplus.com "
            "(matched by name), so there is nothing to follow up",
     "expect": {"needs_followup": False}},

    {"lead": "mitko@pioneerclimate.com",
     "why": "asked whether we have LinkedIn interaction data",
     "expect": {"needs_human": True}},

    {"lead": "deleys@allwayshome.ai",
     "why": "asked for a success-fee structure",
     "expect": {"needs_human": True}},

    # The organizer data settled it: Jake DECLINED both invites; the "cancellations"
    # were us deleting them afterwards. So the declined rule applies (Aakash,
    # Suchit): note the decline as a clash, offer another time, invite as the
    # easier route. Never "still interested" - he said yes once.
    {"lead": "jacob@riverrecords.ai",
     "ideal": "Saw the invite got declined, no problem at all. Feel free to grab another time, or if "
              "it's easier, send me a few slots and I'll get an invite over.",
     "why": "he declined both invites: treat as a clash, offer another time and the invite route",
     "must": [("notes the decline", lambda t: bool(re.search(r"declin", t, re.I))),
              ("offers the invite as alternative", lambda t: "invite" in t.lower())],
     "must_not": [("asks if still interested", still_int),
                  ("says they cancelled", lambda t: bool(re.search(r"(you|they) cancelled|got cancelled", t, re.I)))]},

    {"thread_like": "46-%", "lead": "chinmay@useacceler8.com",
     "ideal": "Absolutely - what window works for you in SGT?",
     "why": "Kal raised a timezone constraint, so ask rather than book around it",
     "must": [("asks about their window", lambda t: bool(re.search(r"window|sgt|singapore|time zone|timezone", t, re.I)))],
     "must_not": [("claims to have booked", lambda t: bool(re.search(r"(had|have) my colleague, Daniel, (book|send)", t, re.I)))]},
]


def run(filt=None):
    conn = store.connect()
    fails, checked = [], 0
    for f in FIXTURES:
        if filt and filt not in f["lead"]:
            continue
        t = (conn.execute("SELECT * FROM threads WHERE thread_id LIKE ? AND lead=?",
                          (f["thread_like"], f["lead"])).fetchone() if f.get("thread_like")
             else conn.execute("SELECT * FROM threads WHERE lead=?", (f["lead"],)).fetchone())
        if t is None:
            fails.append((f["lead"], "thread not in the database")); continue
        em = conn.execute("SELECT * FROM emails WHERE thread_id=? ORDER BY timestamp_email",
                          (t["thread_id"],)).fetchall()
        f["as"] = drafting.sender_name(em)
        f["thread_text"] = " ".join(
            drafting.extract_body(json.loads(r["raw"])).lower().replace(" ", "")
            for r in em if not r["is_outbound"])
        f["thread_urls"] = {u.rstrip(".,)") for r in em
                            for u in re.findall(r"https?://[^\s<>\"\')]+",
                                                json.dumps(json.loads(r["raw"])))}
        try:
            d = drafting.generate(t, em, conn)
        except Exception as exc:
            print(f"FAIL {f['lead'][:30]:30} generate raised: {exc}")
            fails.append((f["lead"], f"generate failed: {exc}")); checked += 1; continue

        bad = []
        for k, v in (f.get("expect") or {}).items():
            if bool(d.get(k)) != v:
                bad.append(f"{k} is {bool(d.get(k))}, expected {v}")
        for name, fn in (f.get("card") or []):
            if not fn(conn, t):
                bad.append(name)
        rec = d["drafts"].get(d["recommended"], "")
        if not d.get("needs_human") and d["needs_followup"]:
            for name, fn in f.get("must", []):
                if not fn(rec):
                    bad.append(f"recommended draft: {name} — failed")
            for name, fn in f.get("must_not", []):
                if fn(rec):
                    bad.append(f"recommended draft: {name}")
        for body in d["drafts"].values():
            for name, fn in f.get("always", []):
                if not fn(body):
                    bad.append(f"always: {name}")
            for name, fn in GLOBAL:
                if not fn(body, f):
                    bad.append(f"global: {name}")
        checked += 1
        words = [len(v.split()) for v in d["drafts"].values()]
        mark = "FAIL" if bad else "ok  "
        print(f"{mark} {f['lead'][:30]:30} {statistics.median(words):>3.0f}w  {f['why'][:52]}")
        for b in sorted(set(bad)):
            print(f"       - {b}")
        if bad:
            fails.append((f["lead"], "; ".join(sorted(set(bad)))))
            print(f"       recommended: {rec.strip()[:200]}")
    print(f"\n{checked - len(fails)}/{checked} fixtures pass")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
