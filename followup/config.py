"""Configuration. Secrets live outside the repo, in mode-600 files in $HOME."""
import os, pathlib, re, logging

TZ = "America/New_York"
MATCH_PHRASE = "gtm whiteglove"      # normalised `contains`, not a prefix
SLA_BUSINESS_DAYS = 2
POLL_OVERLAP_MIN = 5                 # re-query window; upserts make it free
POLL_INTERVAL_SEC = 120
EMAILS_RATE_LIMIT = 20               # requests / minute, hard ceiling
SEND_WINDOW = (8, 17)                # weekday 8am-5pm ET; a click after 5 goes at 8am next weekday
# LIVE: Send puts real mail in front of real leads. DRY_RUN=1 in the environment
# forces a dry run - the safe default for a fresh cloud deployment.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
# The universe is leads Instantly has flagged Interested. Verified against the
# account: i_status 1=interested (94 in whiteglove campaigns), -1=out of office,
# -2=wrong person. 2/3/4 (meeting booked/completed/closed) and -3 (not
# interested) are unused here, so they carry no signal.
INTERESTED = 1
EXCLUDED_STATUSES = (-1, -2, -3)

# The interested set includes the pre-existing backlog, which Sophia is clearing
# by hand. Without a horizon the first run would fire ~94 DMs at once. Older
# threads stay in the store and remain visible via `cli.py due` — they just
# don't ping. Raise or drop these once the backlog is gone.
# Teammates join threads from their real address, not the campaign sending
# account, so `from == eaccount` misreads them as lead replies — the clock never
# resets and we nudge a thread someone just answered. Every sending domain and
# the company domain contain this token; no lead's domain does.
INTERNAL_DOMAIN_TOKEN = "crustdata"

MAX_OVERDUE_DAYS = 5
NOTIFY_BATCH_LIMIT = 15

# In the cloud the database must sit on a PERSISTENT volume; point FOLLOWUP_DB at it.
DB_PATH = pathlib.Path(os.environ.get("FOLLOWUP_DB") or
                       pathlib.Path(__file__).resolve().parent.parent / "followup.db")

# The team's default booking link, sent 135 times. Supplied to the drafter so a
# thread that never contained a link can still offer one — a draft cannot invent a
# URL, and Warren's thread has none in it.
BOOKING_LINK = "https://meetings-na2.hubspot.com/crustdata/demo"

# Where to send someone who has ASKED for more information. Never unprompted —
# every lead here is already interested and does not need the service explained.
WEBSITE_LINK = "https://crustdata.com/solutions/gtm-white-glove"

# Unibox deep link. Verified from the address bar of an open thread:
#   https://app.instantly.ai/app/unibox/13-Kz3b...?status=1&mode=emode_focused
INSTANTLY_THREAD_URL = "https://app.instantly.ai/app/unibox/{thread_id}?status=1&mode=emode_focused"

# Domain fallback is only meaningful for corporate domains. Matching on a free
# provider makes every gmail lead collide with every gmail calendar event —
# measured: four separate leads all "matched" one unrelated meeting.
# Meetings land on whoever hosts them, not on one calendar. Measured across 9
# confirmed bookings: 44% were on Daniel's alone, 77% across the team. Every
# internal calendar is readable by default under Workspace domain sharing.
HOST_CALENDARS = [
    "daniel@crustdata.co", "chris@crustdata.co", "sarah@crustdata.co",
    "anmol@crustdata.co", "doug@crustdata.co", "jack@crustdata.co",
]

# Existing customers sitting in the interested set. Their calendar events are
# recurring account syncs, not demos — they must not generate follow-ups or
# disposition questions.
CUSTOMER_DOMAINS = {
    # confirmed customers — never prospect, never ask for a meeting disposition
    "supergood.ai", "hypernatural.ai", "onghost.com", "greybeam.ai",
    "nemasystems.io", "cedarcopilot.com", "bondtrials.com", "joinbond.com",
    "salad.com", "brew.new", "getbrew.ai", "praxie.com", "natural.com",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "proton.me", "protonmail.com", "pm.me", "gmx.com", "mail.com", "msn.com",
    "hey.com", "fastmail.com", "zoho.com", "yandex.com", "qq.com", "163.com",
}


CRUSTDATA_BASE = "https://api.crustdata.com"
CRUSTDATA_API_VERSION = "2025-11-01"


# Secrets: an environment variable wins (cloud), else a mode-600 file in $HOME
# (laptop). Values are never logged or printed anywhere.
def _secret(env: str, filename: str) -> str | None:
    v = os.environ.get(env)
    if v and v.strip():
        return v.strip()
    p = pathlib.Path.home() / filename
    return p.read_text().strip().lstrip('="\'').strip() if p.exists() else None


def crustdata_key() -> str | None:
    """Optional: without it enrichment is simply skipped."""
    return _secret("CRUSTDATA_KEY", ".crustdata_key")


def instantly_key() -> str:
    k = _secret("INSTANTLY_KEY", ".instantly_key")
    if not k:
        raise SystemExit("no INSTANTLY_KEY env var and no ~/.instantly_key")
    return k


def slack() -> dict:
    """Env: SLACK_BOT_TOKEN (xoxb), SLACK_APP_TOKEN (xapp), SLACK_USER, SLACK_CHANNEL.
    File: ~/.slack_followup with key=value lines (xoxb, xapp, user, channel)."""
    out = {}
    p = pathlib.Path.home() / ".slack_followup"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    for env, key in (("SLACK_BOT_TOKEN", "xoxb"), ("SLACK_APP_TOKEN", "xapp"),
                     ("SLACK_USER", "user"), ("SLACK_CHANNEL", "channel")):
        if os.environ.get(env, "").strip():
            out[key] = os.environ[env].strip()
    return out


# Slack channel IDs start C (public and modern private) or G (legacy private).
# A malformed value falls back to the DM rather than posting nowhere: a typo
# here would otherwise take the whole loop off the air silently.
_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]{7,}$")


def destination(cfg: dict | None = None) -> str:
    """Where cards go: the shared channel if one is configured, else the DM."""
    cfg = slack() if cfg is None else cfg
    ch = (cfg.get("channel") or "").strip()
    if ch and _CHANNEL_RE.match(ch):
        return ch
    if ch:
        logging.getLogger(__name__).warning(
            "ignoring malformed channel=%r in ~/.slack_followup — posting to the DM", ch)
    return cfg["user"]
