"""Instantly v2 client. Sliding-window rate limiting; the emails endpoint is 20/min."""
import json, time, urllib.parse, urllib.request, collections
from . import config

BASE = "https://api.instantly.ai/api/v2"


class RateLimiter:
    """Pace requests evenly rather than bursting. A rolling-window limiter can fire
    all 20 at once and still be "within" 20/min; the endpoint 429s anyway. Even
    spacing at the average rate keeps us under it without ever needing a retry."""

    def __init__(self, n: int, window: float = 60.0, safety: float = 1.08):
        self.interval = (window / n) * safety
        self.last = 0.0

    def take(self):
        wait = self.interval - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()


class Instantly:
    def __init__(self, key: str | None = None):
        self.key = key or config.instantly_key()
        self.limiter = RateLimiter(config.EMAILS_RATE_LIMIT)
        self.request_count = 0

    def get(self, path: str, **params) -> dict:
        self.limiter.take()
        params = {k: v for k, v in params.items() if v is not None}
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.key}",
            # urllib's default UA is rejected by the edge; any normal UA passes.
            "User-Agent": "followup-loop/0.1",
            "Accept": "application/json",
        })
        last_err = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.request_count += 1
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"
                if e.code == 429 or e.code >= 500:
                    time.sleep(5 * (attempt + 1))
                    self.limiter.take()          # retries must re-pace, not bypass
                    continue
                raise RuntimeError(f"{path} -> {last_err}") from e
        raise RuntimeError(f"{path} gave up after 5 attempts -> {last_err}")

    def paginate(self, path: str, **params):
        """Yield every item across pages. `starting_after` is the documented cursor."""
        cursor = None
        while True:
            page = self.get(path, starting_after=cursor, **params)
            items = page.get("items", [])
            yield from items
            cursor = page.get("next_starting_after")
            if not cursor or not items:
                return

    def campaigns(self):
        return self.paginate("/campaigns", limit=100)

    def emails(self, **params):
        """Delta poll. Deliberately NOT filtered by campaign_id — there are 100+
        matching campaigns and one request per campaign would blow the rate limit.
        Campaign membership is decided client-side against the resolved set."""
        return self.paginate("/emails", limit=100, **params)

    def post(self, path: str, payload: dict) -> dict:
        self.limiter.take()
        req = urllib.request.Request(
            f"{BASE}{path}", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                     "User-Agent": "followup-loop/0.1", "Accept": "application/json"},
            method="POST")
        # No retries here, deliberately: a reply must be at-most-once, and a retry
        # after an ambiguous failure is a duplicate email. But the failure must be
        # legible - Instantly's body says WHY (daily limit, bad reply target,
        # account paused) and urllib's default message does not include it.
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self.request_count += 1
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode(errors="replace")
            raise RuntimeError(f"{path} -> HTTP {e.code}: {body}") from e

    def reply(self, eaccount: str, reply_to_uuid: str, subject: str, text: str) -> dict:
        """Reply INTO the existing thread. Sending via our own SMTP would fork it,
        hide the reply from Instantly, and risk the sequence continuing on a lead
        who is mid-conversation with us."""
        return self.post("/emails/reply", {
            "eaccount": eaccount, "reply_to_uuid": reply_to_uuid,
            "subject": subject if subject.lower().startswith("re:") else f"Re: {subject}",
            "body": {"text": text, "html": text.replace("\n", "<br>")}})

    def thread(self, thread_id: str):
        return self.paginate("/emails", limit=100, search=f"thread:{thread_id}")
