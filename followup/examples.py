"""Real follow-ups the team has sent, used as few-shot examples.

Instructions are followed approximately; examples are imitated closely. A prompt
saying "be concise" produced 48-word drafts against a corpus whose median is 35.
Showing five real 35-word emails is what actually fixes it.

The pool starts as history and fills with Sophia's approved sends, so retrieval
shifts over weeks from how the team wrote to what she actually approves.
"""
import json, re
from datetime import datetime, timezone

STOP = set("""a an and are as at be but by for from has have i if in is it its of on or that the
to was we you your our will would can could with this these those there here they them their he
she his her not no do does did been being had how what when where which who whom why us me my am
so than then too very just also about into over out up down again more most some such only own
same s t don now am pm re fwd""".split())


def tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z']{2,}", (text or "").lower()) if w not in STOP}


def build(conn, limit: int = 200) -> dict:
    """Index the most recent hand-written follow-ups, tagged by their thread's motion."""
    from . import motion as M
    from .drafting import extract_body
    rows = conn.execute("""SELECT id, thread_id, lead, raw, timestamp_email
        FROM emails WHERE is_outbound=1 AND ue_type=3
        ORDER BY timestamp_email DESC LIMIT ?""", (limit,)).fetchall()
    motions, n = {}, 0
    for r in rows:
        body = extract_body(json.loads(r["raw"]))
        if not (4 < len(body.split()) < 220):          # skip stubs and essays
            continue
        tid = r["thread_id"]
        if tid not in motions:
            motions[tid] = M.for_thread(conn, tid)
        reply = conn.execute("""SELECT 1 FROM emails WHERE thread_id=? AND is_outbound=0
            AND timestamp_email > ? LIMIT 1""", (tid, r["timestamp_email"])).fetchone()
        conn.execute("""INSERT INTO examples(email_id,thread_id,lead,motion,body,sent_at,
            got_reply,source) VALUES(?,?,?,?,?,?,?,'history')
            ON CONFLICT(email_id) DO UPDATE SET motion=excluded.motion,
            got_reply=excluded.got_reply""",
            (r["id"], tid, r["lead"], motions[tid], body, r["timestamp_email"],
             1 if reply else 0))
        n += 1
    conn.commit()
    return {"indexed": n, "threads": len(motions)}


def record_sent(conn, thread_id: str, body: str, email_id: str | None = None):
    """A draft Sophia approved and sent — the most valuable example there is."""
    from . import motion as M
    from datetime import datetime, timezone
    if not body or len(body.split()) < 4:
        return
    lead = conn.execute("SELECT lead FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
    conn.execute("""INSERT INTO examples(email_id,thread_id,lead,motion,body,sent_at,
        got_reply,source) VALUES(?,?,?,?,?,?,0,'approved')
        ON CONFLICT(email_id) DO UPDATE SET body=excluded.body""",
        (email_id or ("approved:" + thread_id + ":" + datetime.now(timezone.utc).isoformat()),
         thread_id, lead["lead"] if lead else None, M.for_thread(conn, thread_id), body,
         datetime.now(timezone.utc).isoformat()))
    conn.commit()


def retrieve(conn, motion: str, context: str, n: int = 5, allow_still_interested: bool = True) -> list:
    """Examples for this motion, ranked by overlap with what the lead actually wrote.

    Motion alone is a coarse key — a lead asking about pricing needs a different
    reply from one who went quiet. Ranking on word overlap inside the motion gets
    most of that without anyone having to define a taxonomy up front.
    """
    pool = conn.execute("""SELECT body, source, got_reply FROM examples
        WHERE motion=? ORDER BY (source='approved') DESC, got_reply DESC, sent_at DESC
        LIMIT 60""", (motion,)).fetchall()
    # An approved "still interested" send is the strongest example in the pool
    # and the model copies it onto threads that have not earned that check (one
    # unanswered message, not two). Examples must not outrank that rule, so a
    # thread that may not ask does not get shown any example that asks.
    if not allow_still_interested:
        pool = [p for p in pool if not re.search(r"still (interested|keen|up for)", p["body"], re.I)]
    if len(pool) < n:
        pool += conn.execute("""SELECT body, source, got_reply FROM examples
            WHERE motion!=? ORDER BY (source='approved') DESC, sent_at DESC LIMIT ?""",
            (motion, n * 4)).fetchall()
    q = tokens(context)
    scored = []
    for i, r in enumerate(pool):
        overlap = len(q & tokens(r["body"])) if q else 0
        # approved outranks history, then a reply, then similarity, then recency
        scored.append(((r["source"] == "approved", r["got_reply"], overlap, -i), r["body"]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [b for _s, b in scored[:n]]
