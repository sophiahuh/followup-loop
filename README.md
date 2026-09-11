# Follow-Up Loop

Slack bot that chases GTM whiteglove leads who said they were interested and then
went quiet. It polls Instantly, checks the team's Google Calendars, drafts a
follow-up with Claude, posts a card to Slack, and sends the reply through
Instantly when someone clicks Send. Nothing is ever sent without a click.

**Only one copy may run at a time.** Two instances against the same Slack channel
and Instantly account will post every card twice and both can send. When you take
this over, the previous machine's `run.py` must be stopped first.

## Secrets (never in the repo, never in chat)

Mode-600 files in the home directory of whoever runs it:

| file | contents |
|---|---|
| `~/.instantly_key` | Instantly API key |
| `~/.anthropic_key` | Anthropic API key |
| `~/.slack_followup` | `xoxb=...` bot token, `xapp=...` app token, `user=U...`, `channel=C...` |
| `~/.crustdata_key` | Crustdata API key (optional: enables `cli.py enrich`) |
| `~/.gcal_client.json` | Google OAuth client (Internal user type) |
| `~/.gcal_token.json` | created by `python3 cli.py gcal-auth` |

Write them with a silent read so the value never lands in shell history:

    umask 077 && printf 'paste, then Enter: ' && IFS= read -rs k && printf '%s' "$k" > ~/.instantly_key && echo

## First run on a new machine

    pip install -r requirements.txt
    python3 cli.py gcal-auth      # one-time Google consent
    python3 cli.py resolve        # which campaigns count ("gtm whiteglove" in the name)
    python3 cli.py bootstrap      # every interested thread + full history from Instantly
    python3 cli.py gcal-sync      # meetings from the six host calendars
    python3 cli.py enrich --go    # optional, ~2 credits/lead: work-email aliases for gmail leads
    python3 cli.py status

`followup.db` is a cache rebuilt by the steps above. The only things that live
solely in it are outcomes recorded by clicking (had the call / no-show / stop
loop). To carry those over, copy the previous machine's `followup.db` instead of
bootstrapping. It contains lead emails, so treat it as customer data.

## Running

    nohup python3 run.py >> /tmp/followup.log 2>&1 &     # everything
    python3 run.py --ui-only                              # buttons only, no polling/posting

Startup logs `dry_run=... dest=...`. `config.DRY_RUN` must be False to send.

## Day-to-day commands

    python3 cli.py due                        # what is past deadline
    python3 cli.py sweep --days 60 --limit 15 # list backlog behind the 5-day horizon; add --go to post
    python3 cli.py redraft --go               # regenerate every open card after a drafting rule change
    python3 cli.py poll                       # one delta poll
    python3 test_drafts.py                    # draft-quality fixtures (each one is a real Sophia correction)
    python3 test_handlers.py                  # Slack button handlers

## Where the rules live

- `followup/config.py` – every knob: SLA days, send window (8am-5pm ET), horizon,
  host calendars, booking link, customer domains that are never prospected.
- `followup/drafting.py` – the system prompt: email shape, the four-rung ladder,
  invite rules, cancellation wording, and the fact lines computed per thread.
- `followup/motion.py` – where a thread sits in the booking dance (regex, not model).
- `followup/store.py` – schema, migrations, `meeting_state()` (a past confirmed
  meeting is terminal: nobody who had a call is ever followed up).
- `followup/gcal.py` – calendar matching: address, then Crustdata alias, then name.
- `followup/notifier.py` – posting cards, the send loop (at-most-once by design), recap.
- `followup/slack_app.py` – card rendering and button handlers.

## The rules that must not be broken

1. A lead who had a call is never followed up. Enforced at fire time, not click time.
2. Sends are at-most-once: the card is claimed before the network call and never
   retried. "Send did not confirm" means check the Instantly Unibox by hand.
3. Existing customers (`config.CUSTOMER_DOMAINS`) are never prospected.
4. Pricing, contract terms and product-capability questions are flagged "needs you"
   and never answered by the model.

## Deploying to the cloud

The tool is one long-lived process. Slack Socket Mode connects **outbound**, so it
needs no public URL, no inbound port, no webhook. Any small always-on box works:
a Fly.io / Railway / Render worker, a $5 VM, a container with a persistent disk.

What must be true:

1. **Exactly one replica.** Never autoscale it, never run two. Two copies post every
   card twice and both can send. If the platform restarts it, fine; if it runs two
   side by side, not fine.
2. **A persistent volume for `followup.db`.** Set `FOLLOWUP_DB=/data/followup.db`.
   An ephemeral disk loses every recorded outcome on each deploy.
3. **Secrets as environment variables** (the file fallbacks still work on a laptop):

       INSTANTLY_KEY, ANTHROPIC_API_KEY, CRUSTDATA_KEY
       SLACK_BOT_TOKEN (xoxb-...), SLACK_APP_TOKEN (xapp-...), SLACK_USER, SLACK_CHANNEL
       GCAL_TOKEN_JSON  (contents of ~/.gcal_token.json, generated once on a laptop
                         with `python3 cli.py gcal-auth`)  or GCAL_TOKEN_FILE=/data/gcal_token.json
       DRY_RUN=1        (first deploy; remove it once the startup log shows dest=C... and cards look right)

4. **The calendar token belongs to a person.** It is the refresh token of whoever
   ran `gcal-auth`, and it reads the six host calendars through that person's
   access. If they leave or lose access, calendar matching silently stops -
   consider generating it from a shared bot account.
5. **Logs to stdout.** `run.py` logs to stdout; capture it with the platform's log
   viewer. The line `dry_run=... dest=...` on startup is the health check.
6. **Time zone is hard-coded to America/New_York** in `config.py`; the box's own
   zone does not matter.

"Multiple people using it" is already true: everyone in the Slack channel can
send, edit, stop and record outcomes, and each card shows who did. The cloud
changes where the process runs, not who can use it.
