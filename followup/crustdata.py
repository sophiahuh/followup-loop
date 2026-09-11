"""Crustdata person enrichment, used for one thing: finding the work email a
lead may have booked a meeting under when their thread is on a personal domain.

Two hops, because personal-email enrichment is gated on this account:
  gmail -> POST /batch/person/identify      -> LinkedIn URL (+ name, company)
  URL   -> POST /person/contact/enrich      -> business_emails

The result is stored on the thread once and never re-queried. Enriched emails
are ALIASES for the existing address matcher in gcal.sync - they never decide a
match on their own; a wrong enrichment yields no match, not a wrong one.

Cost (verified Sept 3 2026): identify 1 credit per resolved email, business
email +1; unmatched identifiers are free. Rate limit 15/min on enrich.
"""
import json, time, urllib.request, urllib.error
from datetime import datetime, timezone
from . import config

log = __import__("logging").getLogger(__name__)


def _call(method, path, payload=None, key=None):
    key = key or config.crustdata_key()
    if not key:
        raise RuntimeError("no ~/.crustdata_key")
    req = urllib.request.Request(
        f"{config.CRUSTDATA_BASE}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json", "x-api-version": config.CRUSTDATA_API_VERSION},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{path} -> HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from e


def identify(emails: list[str]) -> dict:
    """email -> {name, company, linkedin} for each email that resolves."""
    out = {}
    for i in range(0, len(emails), 25):
        chunk = emails[i:i + 25]
        job = _call("POST", "/batch/person/identify", {"business_emails": chunk})
        bid = job.get("batch_id")
        res = job
        for _ in range(60):                      # ~seconds for known emails
            if str(res.get("status", "")).lower() in ("completed", "done", "finished"):
                break
            time.sleep(2)
            res = _call("GET", f"/batch/{bid}")
        # A completed batch carries no inline results: they are a gzipped JSONL
        # file at a presigned S3 URL (no auth header - the signature is the auth).
        rows = res.get("results") or []
        if not rows and res.get("download_url"):
            import gzip
            raw = urllib.request.urlopen(urllib.request.Request(res["download_url"]), timeout=60).read()
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
            rows = [json.loads(l) for l in raw.decode().splitlines() if l.strip()]
        for row in rows:
            m = (row.get("matches") or [None])[0]
            if not m:
                continue
            pd = m.get("person_data") or {}
            bp = pd.get("basic_profile") or {}
            li = ((pd.get("social_handles") or {}).get("professional_network_identifier") or {}).get("profile_url")
            company = ""
            head = bp.get("headline") or ""
            if "@" in head:
                company = head.split("@", 1)[1].split("|")[0].strip()
            out[row.get("matched_on", "").lower()] = {
                "name": bp.get("name"), "company": company, "linkedin": li,
                "confidence": m.get("confidence_score")}
    return out


def business_emails(linkedin_urls: list[str]) -> dict:
    """linkedin url -> [emails]"""
    out = {}
    for i in range(0, len(linkedin_urls), 25):
        chunk = linkedin_urls[i:i + 25]
        res = _call("POST", "/person/contact/enrich",
                    {"professional_network_profile_urls": chunk, "fields": ["contact.business_emails"]})
        # Shape (verified Sept 3): a list of {matched_on, matches: [{person_data:
        # {contact: {business_emails: [{email, status}]}}}]}.
        rows = res if isinstance(res, list) else res.get("results", res.get("data", []))
        for row in rows:
            url = (row.get("matched_on") or row.get("linkedin_profile_url") or "")
            m = (row.get("matches") or [None])[0] or {}
            contact = ((m.get("person_data") or {}).get("contact")) or row.get("contact") or {}
            emails = list(dict.fromkeys(                       # dedupe, keep order
                e.get("email", "").lower() for e in (contact.get("business_emails") or [])
                if e.get("email") and e.get("status") != "invalid"))
            if url:
                out[url] = emails
        time.sleep(4)                            # 15/min
    return out


def enrich_threads(conn, threads: list, go: bool = False) -> dict:
    """threads: rows with thread_id + lead. Stores aliases; skips anything
    already enriched. With go=False, only reports what it would do."""
    todo = [t for t in threads if not t["enriched_at"]]
    if not go:
        return {"would_enrich": len(todo), "skipped_done": len(threads) - len(todo)}
    now = datetime.now(timezone.utc).isoformat()
    ident = identify([t["lead"] for t in todo])
    urls = [v["linkedin"] for v in ident.values() if v.get("linkedin")]
    mails = business_emails(urls) if urls else {}
    stored = matched = 0
    for t in todo:
        info = ident.get(t["lead"].lower())
        aliases = [e for e in mails.get((info or {}).get("linkedin") or "", []) if e != t["lead"].lower()]
        note = f"{info['name']} @ {info['company']}".strip(" @") if info else None
        conn.execute("UPDATE threads SET enriched_emails=?, enriched_at=?, enrich_note=? WHERE thread_id=?",
                     (json.dumps(aliases), now, note, t["thread_id"]))
        stored += 1
        matched += 1 if aliases else 0
    conn.commit()
    return {"enriched": stored, "with_work_email": matched, "identified": len(ident)}


def enrich_new(conn) -> dict:
    """Hourly job. Interested, active, personal-domain leads with no calendar
    match and no prior enrichment. Bounded to ~2 credits per lead, once. Any
    API failure logs and skips: nothing is stamped as enriched unless both hops
    succeeded, so the lead is simply retried next hour."""
    if not config.crustdata_key():
        return {}
    rows = conn.execute("""SELECT thread_id, lead, enriched_at FROM threads
        WHERE interest_status=1 AND state='ACTIVE' AND lead IS NOT NULL
          AND COALESCE(meeting_state,'none')='none' AND enriched_at IS NULL
        GROUP BY lead""").fetchall()
    rows = [r for r in rows if r["lead"].split("@")[-1].lower() in config.FREE_EMAIL_DOMAINS]
    if not rows:
        return {}
    try:
        res = enrich_threads(conn, rows, go=True)
    except Exception as exc:
        log.warning("enrichment skipped this hour: %s", exc)
        return {"enrich_error": str(exc)[:120]}
    if res.get("with_work_email"):
        # The calendar sync is incremental: it only re-examines events that
        # CHANGED. A fresh alias must be checked against bookings that already
        # exist, so drop the sync tokens and let the next calendar cycle do its
        # bounded 45-day re-read. Only when aliases were actually added.
        from . import store
        for cal in config.HOST_CALENDARS:
            store.set_meta(conn, f"gcal_token:{cal}", "")
        conn.commit()
        res["calendar_resync_queued"] = True
    return res
