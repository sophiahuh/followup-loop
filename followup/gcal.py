"""Google Calendar. Read-only, Daniel's calendar, incremental via syncToken.

All meetings land on daniel@crustdata.co whichever sending persona owns the
thread, and Sophia reads it through her own OAuth token because his calendar is
shared with her at "see all event details" — so no domain-wide delegation and no
second consent flow.
"""
import json, re, pathlib
from datetime import datetime, timedelta, timezone

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
import os
# Cloud: GCAL_TOKEN_FILE / GCAL_CLIENT_FILE point at files on a persistent volume,
# or GCAL_TOKEN_JSON carries the token inline (written to TOKEN_FILE on first
# run so refreshes persist). The token is an "Internal" OAuth user's refresh
# token: it does not expire, but it belongs to one person's Google account.
CLIENT_FILE = pathlib.Path(os.environ.get("GCAL_CLIENT_FILE") or pathlib.Path.home() / ".gcal_client.json")
TOKEN_FILE = pathlib.Path(os.environ.get("GCAL_TOKEN_FILE") or pathlib.Path.home() / ".gcal_token.json")
if os.environ.get("GCAL_TOKEN_JSON") and not TOKEN_FILE.exists():
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(os.environ["GCAL_TOKEN_JSON"])
CALENDAR_ID = "daniel@crustdata.co"   # kept for the auth smoke test


def credentials(interactive: bool = False):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        if not CLIENT_FILE.exists():
            raise SystemExit(f"missing {CLIENT_FILE}")
        creds = InstalledAppFlow.from_client_secrets_file(
            str(CLIENT_FILE), SCOPES).run_local_server(port=0, prompt="consent")
    else:
        raise SystemExit("no calendar token — run `python3 cli.py gcal-auth` once")
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    return creds


def service(interactive: bool = False):
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=credentials(interactive),
                 cache_discovery=False)


def fetch(svc, sync_token: str | None = None, days_back: int = 30):
    """Return (events, next_sync_token).

    showDeleted is essential — without it a cancelled event simply vanishes from
    the response and 'cancelled' is indistinguishable from 'never existed'.
    A 410 means the sync token aged out; fall back to a bounded full resync.
    """
    from googleapiclient.errors import HttpError
    items, page = [], None
    params = {"calendarId": CALENDAR_ID, "showDeleted": True, "maxResults": 250}
    if sync_token:
        params["syncToken"] = sync_token
    else:
        params["timeMin"] = (datetime.now(timezone.utc)
                             - timedelta(days=days_back)).isoformat()
    while True:
        try:
            r = svc.events().list(pageToken=page, **params).execute()
        except HttpError as e:
            if e.resp.status == 410 and sync_token:
                return fetch(svc, None, days_back)     # token expired, resync
            raise
        items += r.get("items", [])
        page = r.get("nextPageToken")
        if not page:
            return items, r.get("nextSyncToken")


def fetch_all(svc, since_iso: str, calendars=None):
    """Every host calendar, flattened to (calendar_id, event)."""
    from . import config
    out = []
    for cid in (calendars or config.HOST_CALENDARS):
        page = None
        while True:
            r = svc.events().list(calendarId=cid, showDeleted=True, maxResults=250,
                                  pageToken=page, timeMin=since_iso).execute()
            out += [(cid, e) for e in r.get("items", [])]
            page = r.get("nextPageToken")
            if not page:
                break
    return out


def thread_addresses(conn, thread_id: str, lead: str | None) -> set:
    """Every address that could represent this lead on an invite.

    The targeted address is often not the one that shows up on the calendar —
    one thread is addressed to elijahmurray@gmail.com but the person actually
    corresponding, and booking, is beste@theraisecompany.ai.
    """
    addrs = {a.lower() for a in ([lead] if lead else []) if a}
    for r in conn.execute("""SELECT DISTINCT from_email FROM emails
                             WHERE thread_id=? AND is_outbound=0""", (thread_id,)):
        if r["from_email"]:
            addrs.add(r["from_email"].lower())
    # Work addresses Crustdata knows for this person. Aliases only: they let an
    # exact address match happen that otherwise could not.
    row = conn.execute("SELECT enriched_emails FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
    if row and row["enriched_emails"]:
        addrs |= {e.lower() for e in json.loads(row["enriched_emails"]) if e}
    return addrs


def enriched_addresses(conn) -> set:
    out = set()
    for r in conn.execute("SELECT enriched_emails FROM threads WHERE enriched_emails IS NOT NULL"):
        out |= {e.lower() for e in json.loads(r["enriched_emails"]) if e}
    return out


# --- name fallback ----------------------------------------------------------
# A lead can book under an address that appears in no email (Michael wrote from
# gmail and booked as michael@atomic.supply), so address matching alone misses
# real meetings. Names are the other signal the event carries. Deliberately
# strict: needs a FULL name on the event, the first name must be how we greet
# the lead, and the surname must appear in the lead's own address or signature.
# Measured on 11 unmatched demos: this matched Michael and nobody else, where
# first-name-only would have matched three different Joes.
_DEMO_TITLE = re.compile(
    r"demo\s+(?:between\s+\w+\s+and|for|with)\s+(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
    r"|^(?P<host>[A-Z][a-z]+)(?:\s+[A-Z][a-z]+)?\s+and\s+(?P<name2>[A-Z][a-z]+\s+[A-Z][a-z]+)\s*$",
    re.I)


def guest_name(event) -> tuple[str, str] | None:
    """(first, last) for the external guest, from the title or displayName.
    None unless a full name is present - a first name alone is not enough."""
    for a in event.get("attendees", []):
        dn = (a.get("displayName") or "").strip()
        if dn and "crustdata" not in (a.get("email") or "") and len(dn.split()) >= 2:
            parts = dn.split()
            return parts[0].lower(), parts[-1].lower()
    m = _DEMO_TITLE.search((event.get("summary") or "").strip())
    if m:
        name = m.group("name") or m.group("name2")
        if name and len(name.split()) >= 2:
            parts = name.split()
            return parts[0].lower(), parts[-1].lower()
    return None


def looks_like_demo(event) -> bool:
    summ = (event.get("summary") or "").lower()
    desc = (event.get("description") or "").lower()
    return "demo" in summ or "calendly.com/events" in desc or bool(_DEMO_TITLE.search(event.get("summary") or ""))


def name_index(conn) -> dict:
    """first name we greet the lead by -> [(thread_id, lead, surname_evidence)].
    surname_evidence is the lead address plus the tail of every inbound body,
    which is where a signature lives."""
    from .drafting import extract_body
    out = {}
    for t in conn.execute("""SELECT thread_id, lead FROM threads
                             WHERE lead IS NOT NULL AND interest_status=1"""):
        first = None
        evidence = [t["lead"].lower()]
        for r in conn.execute("SELECT raw, is_outbound FROM emails WHERE thread_id=?",
                              (t["thread_id"],)):
            body = extract_body(json.loads(r["raw"]))
            if r["is_outbound"] and first is None:
                m = re.match(r"\s*(?:hey|hi|hello)\s+([A-Za-z]+)", body, re.I)
                if m:
                    first = m.group(1).lower()
            elif not r["is_outbound"]:
                evidence.append(body.strip()[-400:].lower())
        if first:
            out.setdefault(first, []).append((t["thread_id"], t["lead"], " ".join(evidence)))
    return out


def name_match(event, names: dict):
    """(guest_email, thread_id, lead) or None. Exactly one candidate must survive."""
    gn = guest_name(event)
    if not gn or not looks_like_demo(event):
        return None
    first, last = gn
    cands = [(tid, lead) for tid, lead, ev in names.get(first, []) if last in ev]
    if len(cands) != 1:
        return None
    guest = next((a.get("email", "").lower() for a in event.get("attendees", [])
                  if "crustdata" not in (a.get("email") or "")), "")
    return guest, cands[0][0], cands[0][1]


def sync(conn, svc, days_back: int = 45) -> dict:
    """Pull events from every host calendar and record the ones that match a
    tracked thread. Incremental per calendar via syncToken; a 410 falls back to a
    bounded resync. showDeleted keeps cancellations visible — without it a
    cancelled meeting is indistinguishable from one that never existed."""
    from googleapiclient.errors import HttpError
    from . import config, store
    from datetime import datetime, timedelta, timezone

    index = {}
    for t in conn.execute("SELECT thread_id, lead FROM threads WHERE lead IS NOT NULL"):
        for a in thread_addresses(conn, t["thread_id"], t["lead"]):
            index.setdefault(a, (t["thread_id"], t["lead"]))
    names = name_index(conn)
    enriched = enriched_addresses(conn)

    now = datetime.now(timezone.utc).isoformat()
    seen = matched = 0
    for cid in config.HOST_CALENDARS:
        tok = store.get_meta(conn, f"gcal_token:{cid}")
        params = {"calendarId": cid, "showDeleted": True, "maxResults": 250}
        if tok:
            params["syncToken"] = tok
        else:
            params["timeMin"] = (datetime.now(timezone.utc)
                                 - timedelta(days=days_back)).isoformat()
        page, items = None, []
        while True:
            try:
                r = svc.events().list(pageToken=page, **params).execute()
            except HttpError as e:
                if e.resp.status == 410:                 # token aged out
                    store.set_meta(conn, f"gcal_token:{cid}", "")
                    params.pop("syncToken", None)
                    params["timeMin"] = (datetime.now(timezone.utc)
                                         - timedelta(days=days_back)).isoformat()
                    page = None
                    continue
                raise
            items += r.get("items", [])
            page = r.get("nextPageToken")
            if not page:
                if r.get("nextSyncToken"):
                    store.set_meta(conn, f"gcal_token:{cid}", r["nextSyncToken"])
                break

        for e in items:
            seen += 1
            hit = None
            for a in e.get("attendees", []):
                em = (a.get("email") or "").lower()
                if em in index:
                    hit = (em, a.get("responseStatus"), index[em]); break
            mtype = None
            if not hit:
                nm = name_match(e, names)
                if not nm:
                    continue
                em, tid, lead = nm
                resp = next((a.get("responseStatus") for a in e.get("attendees", [])
                             if (a.get("email") or "").lower() == em), None)
                mtype = "name"
            else:
                em, resp, (tid, lead) = hit
                if em in enriched and em != (lead or "").lower():
                    mtype = "enriched"
            from .clock import to_utc
            st = e.get("start") or {}; en = e.get("end") or {}
            _desc = (e.get("description") or "").lower()
            tool = ("hubspot" if "hubspot.com/meetings" in _desc
                    else "calendly" if "calendly.com" in _desc else "manual")
            conn.execute("""INSERT INTO meetings(event_id,calendar_id,thread_id,lead,
                matched_address,match_type,summary,start_at,end_at,status,response_status,
                updated_at,event_updated,organizer,booking_tool) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,
                  response_status=excluded.response_status, start_at=excluded.start_at,
                  end_at=excluded.end_at, summary=excluded.summary,
                  updated_at=excluded.updated_at, event_updated=excluded.event_updated,
                  organizer=excluded.organizer, booking_tool=excluded.booking_tool""",
                (e["id"], cid, tid, lead, em,
                 mtype or ("exact" if em == (lead or "").lower() else "alt"),
                 e.get("summary"), to_utc(st.get("dateTime") or st.get("date")),
                 to_utc(en.get("dateTime") or en.get("date")), e.get("status"), resp, now,
                 to_utc(e.get("updated")), ((e.get("organizer") or {}).get("email") or "").lower(),
                 tool))
            matched += 1
    conn.commit()
    return {"events_seen": seen, "meetings_matched": matched}
