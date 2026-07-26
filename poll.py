#!/usr/bin/env python3
"""Polls a JSON endpoint and notifies configured channels when availability changes."""

import base64
import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone
from email.mime.text import MIMEText


def env_str(name, default=""):
    """Env var as a string, treating unset AND empty/blank as 'use the default'.

    The CI runner sets an env entry to "" when the backing secret/variable
    doesn't exist, so plain os.environ.get() defaults never kick in.
    """
    value = os.environ.get(name, "").strip()
    return value if value else default


TARGET_ID = env_str("TARGET_ID")
TARGET_NAME = env_str("TARGET_NAME", "target")
TARGET_URL = env_str("TARGET_URL")
TARGET_API_URL = env_str("TARGET_API_URL")
_deadline = env_str("TARGET_DEADLINE_UTC")  # e.g. 2099-01-01T00:00:00Z
WATCH_END_UTC = (
    datetime.fromisoformat(_deadline.replace("Z", "+00:00")) if _deadline else None
)

STATE_FILE = env_str("STATE_FILE", ".cache")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

FAILURE_WARN_THRESHOLD = 3  # consecutive failed runs before warning ping


def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # narrow Windows consoles; CI runners are UTF-8
        print(line.encode("ascii", "replace").decode(), flush=True)


# --------------------------------------------------------------------------- #
# Notification channels. Each is active only if its secrets are configured.
# --------------------------------------------------------------------------- #

def http_post(url, data, headers, timeout=20):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def send_discord(title, body, urgent):
    webhook = env_str("DISCORD_WEBHOOK_URL")
    if not webhook:
        log("discord: not configured")
        return False
    mention = env_str("DISCORD_MENTION", "@everyone")
    if mention.lower() == "none":  # explicit opt-out of pinging
        mention = ""
    payload = {
        "content": f"{mention} {title}" if urgent and mention else title,
        "embeds": [
            {
                "title": TARGET_NAME,
                "url": TARGET_URL or None,
                "description": body,
                "color": 0x00CC52 if urgent else 0x8899AA,
            }
        ],
    }
    status, _ = http_post(
        webhook,
        json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    log(f"discord: sent (HTTP {status})")
    return True


def smtp_send(to_addrs, subject, body):
    host = env_str("SMTP_HOST", "smtp.gmail.com")
    port = int(env_str("SMTP_PORT", "465"))
    user = env_str("SMTP_USER")
    password = env_str("SMTP_PASS")
    if not (user and password and to_addrs):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to_addrs)
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=ssl.create_default_context())
    with server:
        server.login(user, password)
        server.sendmail(user, to_addrs, msg.as_string())
    return True


def send_email(subject, body):
    to = [a.strip() for a in env_str("EMAIL_TO").split(",") if a.strip()]
    if not to:
        log("email: not configured")
        return False
    if smtp_send(to, subject, body):
        log(f"email: sent to {len(to)} recipient(s)")
        return True
    log("email: SMTP_USER/SMTP_PASS not configured")
    return False


def send_sms(short_message):
    """Text message via any configured provider: carrier email gateway, Textbelt, Twilio."""
    sent = False

    gateways = [a.strip() for a in env_str("SMS_EMAIL_GATEWAY").split(",") if a.strip()]
    if gateways:
        if smtp_send(gateways, "", short_message):
            log(f"sms: sent via carrier email gateway to {len(gateways)} number(s)")
            sent = True
        else:
            log("sms: SMS_EMAIL_GATEWAY set but SMTP not configured")

    textbelt_key = env_str("TEXTBELT_KEY")
    textbelt_phone = env_str("TEXTBELT_PHONE")
    if textbelt_key and textbelt_phone:
        data = urllib.parse.urlencode(
            {"phone": textbelt_phone, "message": short_message, "key": textbelt_key}
        ).encode()
        status, resp = http_post(
            "https://textbelt.com/text", data,
            {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        )
        log(f"sms: textbelt HTTP {status}: {resp[:120]}")
        sent = sent or '"success": true' in resp or '"success":true' in resp

    sid = env_str("TWILIO_ACCOUNT_SID")
    token = env_str("TWILIO_AUTH_TOKEN")
    tw_from = env_str("TWILIO_FROM")
    tw_to = env_str("TWILIO_TO")
    if sid and token and tw_from and tw_to:
        data = urllib.parse.urlencode(
            {"From": tw_from, "To": tw_to, "Body": short_message}
        ).encode()
        auth = b64encode(f"{sid}:{token}".encode()).decode()
        status, _ = http_post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {auth}",
                "User-Agent": USER_AGENT,
            },
        )
        log(f"sms: twilio HTTP {status}")
        sent = True

    if not (gateways or (textbelt_key and textbelt_phone) or sid):
        log("sms: not configured")
    return sent


def notify_all(title, body, short_message):
    """Full urgent alert on every channel. Failures in one channel don't block others."""
    for fn, args in (
        (send_discord, (title, body, True)),
        (send_email, (title, body)),
        (send_sms, (short_message,)),
    ):
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001 - a dead channel must not kill the alert
            log(f"notify error in {fn.__name__}: {type(e).__name__}: {e}")


def notify_info(title, body):
    """Low-priority status ping, Discord only."""
    try:
        send_discord(title, body, urgent=False)
    except Exception as e:  # noqa: BLE001
        log(f"notify error in send_discord: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #

def fetch_target():
    api = urllib.parse.urlsplit(TARGET_API_URL)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{api.scheme}://{api.netloc}/checkout-external?eid={TARGET_ID}",
    }
    last_err = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(TARGET_API_URL, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
            data = json.loads(raw)
            if "tickets" not in data and "ticketAvailabilityInfo" not in data:
                raise ValueError(f"unexpected response shape: keys={sorted(data)[:8]}")
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"fetch attempt {attempt} failed: {type(e).__name__}")
            if attempt < 3:
                time.sleep(10 * attempt)
    raise RuntimeError(f"all fetch attempts failed: {type(last_err).__name__}: {last_err}")


def fmt_price(item):
    for key in ("cost", "total_cost"):
        d = (item.get(key) or {}).get("display")
        if d:
            if d.endswith(" USD"):
                return "$" + d[:-4]
            return d
    return "free/unpriced"


def parse_state(data):
    items = data.get("tickets") or []
    info = data.get("ticketAvailabilityInfo") or {}
    classes = {}
    available = []
    for t in items:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "Unnamed")
        status = str(t.get("on_sale_status") or "UNKNOWN")
        hidden = bool((t.get("characteristics") or {}).get("is_hidden"))
        entry = {"status": status, "price": fmt_price(t), "hidden": hidden}
        classes[name] = entry
        if status == "AVAILABLE" and not hidden:
            available.append((name, entry["price"]))

    fingerprint = {
        "has_access_code": bool((data.get("event") or {}).get("has_access_code")),
        "is_sold_out": bool(info.get("is_sold_out")),
        "has_available_tickets": bool(info.get("has_available_tickets")),
        "has_available_hidden_tickets": bool(info.get("has_available_hidden_tickets")),
        "waitlist_enabled": bool(info.get("waitlist_enabled")),
        "remaining_capacity": info.get("remaining_capacity"),
        "classes": classes,
    }
    generic_available = bool(info.get("has_available_tickets")) or (
        isinstance(info.get("remaining_capacity"), int) and info["remaining_capacity"] > 0
    )
    return fingerprint, available, generic_available


def describe_change(old, new):
    if not old:
        return ["First successful check."]
    lines = []
    for flag in (
        "has_access_code",
        "is_sold_out",
        "has_available_tickets",
        "has_available_hidden_tickets",
        "waitlist_enabled",
        "remaining_capacity",
    ):
        if old.get(flag) != new.get(flag):
            lines.append(f"{flag}: {old.get(flag)} -> {new.get(flag)}")
    oc, nc = old.get("classes") or {}, new.get("classes") or {}
    for name in nc:
        if name not in oc:
            lines.append(f'Listed: "{name}" — {nc[name]["status"]} ({nc[name]["price"]})')
        elif oc[name]["status"] != nc[name]["status"]:
            lines.append(f'"{name}": {oc[name]["status"]} -> {nc[name]["status"]}')
    for name in oc:
        if name not in nc:
            lines.append(f'Delisted: "{name}"')
    return lines


# --------------------------------------------------------------------------- #
# State persistence (base64-wrapped JSON so the committed file isn't grep-bait)
# --------------------------------------------------------------------------- #

DEFAULT_STATE = {
    "schema": 1,
    "keepalive": "",
    "ended": False,
    "consecutive_failures": 0,
    "failure_warned": False,
    "alerted_generic": False,
    "alerted_names": [],
    "fingerprint": None,
}


def serialize(state):
    return json.dumps(state, indent=1, sort_keys=True)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = f.read().strip()
        state = json.loads(base64.b64decode(raw).decode("utf-8"))
        return {**DEFAULT_STATE, **state}
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return dict(DEFAULT_STATE)


def commit_state():
    """Commit + push the state file from inside a long-running job, so state
    survives even if the runner dies mid-loop. Best-effort; never raises."""
    def git(*args):
        return subprocess.run(["git", *args], check=False, capture_output=True, text=True)

    try:
        if not git("status", "--porcelain", STATE_FILE).stdout.strip():
            return
        git("config", "user.name", "github-actions[bot]")
        git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        git("add", STATE_FILE)
        git("commit", "-m", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        git("pull", "--rebase", "origin", "main")
        rc = git("push", "origin", "HEAD:main").returncode
        log(f"state: committed (push rc={rc})")
    except Exception as e:  # noqa: BLE001
        log(f"state commit failed: {type(e).__name__}")


def save_state(state, previous_serialized):
    serialized = serialize(state)
    if serialized != previous_serialized:
        encoded = base64.b64encode(serialized.encode("utf-8")).decode("ascii")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(encoded + "\n")
        log("state: updated on disk")
        if env_str("COMMIT_ON_SAVE") == "true":
            commit_state()
    else:
        log("state: unchanged")


def set_github_output(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build_alert(available, generic_only):
    if available:
        lines = [f"• {name} — {price}" for name, price in available]
        names_short = ", ".join(name for name, _ in available)
    else:
        lines = ["• (Type not identified — availability reported at event level)"]
        names_short = "unknown type"
    title = f"🎟️ AVAILABLE NOW — {TARGET_NAME}"
    body = (
        "On sale RIGHT NOW:\n"
        + "\n".join(lines)
        + f"\n\nBuy here → {TARGET_URL}\n"
        + f"(Checked {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — move fast.)"
    )
    # SMS: same full URL as the email (it deep-links into the app; shortened
    # forms don't), and it goes FIRST — carrier gateways prepend sender/subject
    # text that counts against the segment limit, so anything near the end
    # risks being split off. ASCII only (non-GSM chars force 70-char segments).
    label = f"TICKETS: {names_short}".encode("ascii", "replace").decode()
    if len(label) > 45:
        label = label[:42] + "..."
    short = f"{TARGET_URL} {label}"
    if generic_only and not available:
        title = f"🎟️ May be available — {TARGET_NAME}"
    return title, body, short


def run_check(state):
    """One availability check; mutates state (alert bookkeeping, failure counters)."""
    try:
        data = fetch_target()
    except RuntimeError as e:
        state["consecutive_failures"] = min(state["consecutive_failures"] + 1, FAILURE_WARN_THRESHOLD + 1)
        log(f"check failed ({state['consecutive_failures']} in a row)")
        if state["consecutive_failures"] >= FAILURE_WARN_THRESHOLD and not state["failure_warned"]:
            state["failure_warned"] = True
            notify_info(
                f"⚠️ Watcher failing — {TARGET_NAME}",
                f"{state['consecutive_failures']} consecutive checks have failed "
                f"(last error: {e}).\nThe source may have changed or blocked the API. "
                f"Check the run logs, and check manually: {TARGET_URL}",
            )
        return

    if state["consecutive_failures"]:
        if state["failure_warned"]:
            notify_info(f"✅ Watcher recovered — {TARGET_NAME}", "Checks are succeeding again.")
        state["consecutive_failures"] = 0
        state["failure_warned"] = False

    fingerprint, available, generic_available = parse_state(data)
    available_names = [name for name, _ in available]
    log(
        f"check ok: {len(fingerprint['classes'])} listed, {len(available)} available, "
        f"open={fingerprint['has_available_tickets']}, gated={fingerprint['has_access_code']}"
    )

    newly_available = [n for n in available_names if n not in state["alerted_names"]]
    generic_new = generic_available and not available and not state["alerted_generic"]

    if newly_available or generic_new:
        title, body, short = build_alert(available, generic_only=not available)
        log(f"ALERT: {len(available)} newly available")
        notify_all(title, body, short)
    elif fingerprint != state["fingerprint"]:
        changes = describe_change(state["fingerprint"], fingerprint)
        log(f"state change: {len(changes)} difference(s)")
        notify_info(
            f"ℹ️ Page changed — {TARGET_NAME}",
            "\n".join(changes) + f"\n\nNothing purchasable yet. {TARGET_URL}",
        )

    state["alerted_names"] = available_names
    state["alerted_generic"] = generic_available and not available
    state["fingerprint"] = fingerprint


def main():
    if not TARGET_API_URL:
        log("TARGET_API_URL not configured; nothing to do")
        sys.exit(1)

    state = load_state()
    previous_serialized = serialize(state)
    now = datetime.now(timezone.utc)

    if os.environ.get("TEST_NOTIFY") == "true" or "--test" in sys.argv:
        log("TEST_NOTIFY: sending test alert on all configured channels")
        title, body, short = build_alert([("Sample item [TEST]", "$100.00")], False)
        notify_all("[TEST] " + title, "This is a test of the watcher. " + body,
                   "[TEST] " + short)
        return

    if WATCH_END_UTC and now >= WATCH_END_UTC:
        if not state["ended"]:
            state["ended"] = True
            notify_info(
                f"🏁 Watch ended — {TARGET_NAME}",
                "The deadline has passed. This watcher is shutting itself off.",
            )
            save_state(state, previous_serialized)
        set_github_output("watch_ended", "true")
        log("watch period over; nothing to do")
        return

    # Monthly keepalive commit so the scheduler isn't auto-disabled after 60 idle days.
    month = now.strftime("%Y-%m")
    if state["keepalive"] != month:
        state["keepalive"] = month

    # The scheduler throttles 5-minute crons to roughly hourly, so each
    # invocation polls continuously until LOOP_SECONDS runs out (0 = one
    # check) and the next queued run takes over on exit. State changes are
    # persisted the moment they happen, not at the end of the run.
    loop_seconds = int(env_str("LOOP_SECONDS", "0"))
    interval = max(30, int(env_str("LOOP_INTERVAL", "60")))
    deadline = time.monotonic() + loop_seconds
    while True:
        if WATCH_END_UTC and datetime.now(timezone.utc) >= WATCH_END_UTC:
            state["ended"] = True
            notify_info(
                f"🏁 Watch ended — {TARGET_NAME}",
                "The deadline has passed. This watcher is shutting itself off.",
            )
            set_github_output("watch_ended", "true")
            break
        run_check(state)
        serialized = serialize(state)
        if serialized != previous_serialized:
            save_state(state, previous_serialized)
            previous_serialized = serialized
        if time.monotonic() + interval > deadline:
            break
        time.sleep(interval)
    save_state(state, previous_serialized)


if __name__ == "__main__":
    main()
