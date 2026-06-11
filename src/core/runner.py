#!/usr/bin/env python3
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "state.json"

# all source-specific config comes from the environment (set as repo secrets)
BASE = os.environ.get("SOURCE_BASE", "").rstrip("/")
SOURCE_ID = "69d4ca8b43d279df726b8c5c"
REWARD_FIELD = os.environ.get("REWARD_FIELD", "")
SOURCE_URL = (
    f"{BASE}/next/v4/participant/matching/projects/"
    f"search/profiles/{SOURCE_ID}?page=1&pageSize=50&sort=publishedAt&showEligible=true"
)
REF_URL = f"{BASE}/next/participants/projects?sort=publishedAt&eligible=true"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"

# Transient failures (timeout / 5xx) self-recover: state.json is only advanced on a
# successful poll, so a missed poll delays detection, it never drops an entry. So we
# stay quiet for a couple of misses, then raise ONE alarm if it really looks down,
# and re-remind occasionally while it stays down (so a long outage isn't one
# missable email). Counters live in state.json because the runner is ephemeral.
FAIL_ALERT_THRESHOLD = 3
FAIL_REALERT_EVERY = 12


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_cookies():
    raw = os.environ.get("AUTH_COOKIES", "").strip()
    if not raw:
        msg = "Scheduled digest (GH Actions): AUTH_COOKIES secret is missing or empty. Set it via `gh secret set AUTH_COOKIES`."
        log(msg)
        send_email("Scheduled digest: secret missing", msg)
        sys.exit(1)
    return raw


def extract_xsrf(cookies):
    m = re.search(r"XSRF-TOKEN=([^;]+)", cookies)
    return m.group(1) if m else ""


def fetch_items(cookies, attempts=3):
    xsrf = extract_xsrf(cookies)
    req = urllib.request.Request(
        SOURCE_URL,
        method="GET",
        headers={
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "cookie": cookies,
            "referer": REF_URL,
            "user-agent": UA,
            "x-xsrf-token": xsrf,
        },
    )
    backoffs = [2, 5, 10]
    status, body = -1, "no attempt made"
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            status = e.code
            # auth errors won't fix themselves on retry; surface immediately
            if status in (401, 403):
                return status, body
        except Exception as e:
            status, body = -1, str(e)
        if i < attempts - 1:
            log(f"attempt {i + 1}/{attempts} failed (status={status}); retrying in {backoffs[i]}s")
            time.sleep(backoffs[i])
    return status, body


def item_url(p):
    pid = p.get("id", "")
    slug = re.sub(r"[^a-z0-9-]+", "-", (p.get("name") or "").lower()).strip("-")[:80]
    return f"{BASE}/next/participants/projects/{pid}/{slug}" if pid else REF_URL


def summarize(p):
    name = p.get("name", "(untitled)")
    reward = p.get(REWARD_FIELD, "?")
    minutes = p.get("timeMinutesRequired", "?")
    return f"${reward} / {minutes}min - {name}"


def format_ist(iso_ts):
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone(IST).strftime("%Y-%m-%d %I:%M %p IST")
    except Exception:
        return iso_ts


def send_email(subject, body):
    to_addr = os.environ.get("EMAIL_TO", "")
    from_addr = os.environ.get("EMAIL_FROM", "")
    app_pw = os.environ.get("EMAIL_APP_PASSWORD", "")
    if not (to_addr and from_addr and app_pw):
        log("EMAIL_TO/EMAIL_FROM/EMAIL_APP_PASSWORD missing")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
            s.login(from_addr, app_pw)
            s.send_message(msg)
        log(f"email sent to {to_addr}")
    except Exception as e:
        log(f"email failed: {e}")


def send_push(title, body, priority="max", tags="loudspeaker"):
    # Push to ntfy.sh so a new item actually rings (email is silent). The topic is a
    # secret because ntfy topic URLs are public; a random topic keeps it private.
    # priority=max + a default alert tone rings through silent/DND on the phone app.
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Priority": priority,
            "Tags": tags,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        log("push sent to ntfy topic")
    except Exception as e:
        log(f"push failed: {e}")


def load_seen():
    if not SEEN_FILE.exists():
        return {"first_run": True, "ids": []}
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {"first_run": True, "ids": []}


SEEN_CAP = 5000


def save_seen(ids, first_run=False, fails=0, auth_alerted=False):
    # Keep newest IDs only. ObjectIDs sort lexicographically by timestamp, so sorted
    # desc puts recent ones first; take top SEEN_CAP.
    capped = sorted(set(ids), reverse=True)[:SEEN_CAP]
    SEEN_FILE.write_text(
        json.dumps(
            {"first_run": first_run, "fails": fails, "auth_alerted": auth_alerted, "ids": sorted(capped)},
            indent=2,
        )
    )


def note_transient_failure(status, body):
    # A timeout / 5xx / parse blip. Don't fail CI and don't spam: count it, and only
    # email once we've missed FAIL_ALERT_THRESHOLD in a row (a real outage), then
    # re-remind every FAIL_REALERT_EVERY. The seen ids are left untouched so the next
    # good poll still detects anything new. Exit 0 -> no GitHub "run failed" mail.
    seen = load_seen()
    fails = seen.get("fails", 0) + 1
    save_seen(
        seen.get("ids", []),
        first_run=seen.get("first_run", True),
        fails=fails,
        auth_alerted=seen.get("auth_alerted", False),
    )
    log(f"transient failure #{fails}: status={status}, {str(body)[:200]}")
    if fails == FAIL_ALERT_THRESHOLD or (
        fails > FAIL_ALERT_THRESHOLD and (fails - FAIL_ALERT_THRESHOLD) % FAIL_REALERT_EVERY == 0
    ):
        send_email(
            "Scheduled digest: looks down",
            f"{fails} consecutive failed polls; the job may be down. Check the "
            f"AUTH_COOKIES secret and the GitHub Actions runs (the source may also "
            f"be blocking the runner). Last error: status={status}, {str(body)[:200]}",
        )
    sys.exit(0)


def main():
    cookies = get_cookies()
    status, body = fetch_items(cookies)

    if status in (401, 403):
        # Auth is dead and won't self-heal. Alarm ONCE (dedup via auth_alerted) so you
        # aren't pinged every run until the cookie is refreshed. Exit 0 to keep CI
        # quiet; the email is the signal. auth_alerted resets on the next good poll.
        seen = load_seen()
        msg = "Scheduled digest (GH Actions): auth expired. Update the AUTH_COOKIES secret."
        log(msg)
        if not seen.get("auth_alerted", False):
            send_email("Scheduled digest: auth expired", msg)
        save_seen(
            seen.get("ids", []),
            first_run=seen.get("first_run", True),
            fails=seen.get("fails", 0),
            auth_alerted=True,
        )
        sys.exit(0)

    if status != 200:
        note_transient_failure(status, body)

    try:
        data = json.loads(body)
    except Exception as e:
        note_transient_failure(f"{status}/parse", f"{e}; body head: {body[:200]}")

    items = data.get("results", []) or []
    log(f"fetched {len(items)} items (page {data.get('page')}, total page-size {data.get('pageSize')})")

    current = {str(p["id"]): p for p in items if p.get("id")}

    seen = load_seen()
    seen_ids = set(seen.get("ids", []))
    first_run = seen.get("first_run", True)

    if first_run:
        log(f"first run: recording {len(current)} known items, suppressing notification")
        save_seen(seen_ids | set(current.keys()), first_run=False)
        return

    new_ids = [pid for pid in current if pid not in seen_ids]
    if not new_ids:
        log(f"no new items ({len(current)} known)")
        save_seen(seen_ids | set(current.keys()), first_run=False)
        return

    new_ids.sort(key=lambda pid: current[pid].get("publishedAt", ""), reverse=True)

    log(f"NEW: {len(new_ids)}")
    for pid in new_ids:
        p = current[pid]
        s = summarize(p)
        url = item_url(p)
        log(f"  + {s}  ({url})")
        send_email(
            f"[Feed] {s}",
            f"{s}\n\n{p.get('description', '')[:500]}\n\nView: {url}\nPublished: {format_ist(p.get('publishedAt'))}",
        )
        send_push(f"[Feed] {s}", f"{s}\n\n{url}")

    save_seen(seen_ids | set(current.keys()), first_run=False)


if __name__ == "__main__":
    main()
