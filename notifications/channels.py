"""
channels.py
-----------
External notification channels: Telegram, Slack (incoming webhook), Email (SMTP).
Console output stays in change_intel/alert_router.py.

Config (env vars or a .env file, see .env.example). A channel with missing
config is silently skipped, so nothing breaks if you only set up one.

Severity tiers (who gets an immediate alert):
    high   -> telegram + slack + email
    medium -> telegram + slack
    low    -> none (goes to the daily digest)
"""

import os
import smtplib
import logging
from email.message import EmailMessage

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TIERS = {
    "high": ["telegram", "slack", "email"],
    "medium": ["telegram", "slack"],
    "low": [],
}
ALL_CHANNELS = ["telegram", "slack", "email"]


def is_configured(channel: str) -> bool:
    if channel == "telegram":
        return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    if channel == "slack":
        return bool(os.getenv("SLACK_WEBHOOK_URL"))
    if channel == "email":
        return bool(os.getenv("SMTP_HOST") and os.getenv("ALERT_EMAIL_TO"))
    return False


def configured_channels() -> list[str]:
    return [c for c in ALL_CHANNELS if is_configured(c)]


# ── formatting ───────────────────────────────────────────────────────────────

def format_alert(change: dict) -> tuple[str, str]:
    """Returns (subject, plain-text body). Always carries claim IDs as evidence."""
    diff = change.get("_diff", {})
    sev = str(change.get("severity", "low")).upper()
    stealth = " [STEALTH]" if change.get("is_stealth") else ""
    subject = (f"IntelTrack {sev}{stealth}: {change.get('_competitor', '?')} "
               f"{change.get('field')} changed")
    body = (
        f"IntelTrack alert [{sev}]{stealth}\n"
        f"Competitor : {change.get('_competitor', '?')}\n"
        f"Market     : {change.get('_market', '?')}\n"
        f"Plan/SKU   : {diff.get('plan_or_sku', '?')}\n"
        f"Field      : {change.get('field')}\n"
        f"Change     : {diff.get('old_value')} -> {diff.get('new_value')}\n"
        f"Type       : {change.get('change_type')}\n"
        f"Change ID  : {change.get('id')}\n"
        f"Evidence   : {change.get('claim_id_old', '-')} -> {change.get('claim_id_new', '-')}\n"
    )
    return subject, body


# ── senders (each returns True on success, never raises) ────────────────────

def _telegram(subject: str, text: str) -> bool:
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage",
            json={"chat_id": os.getenv("TELEGRAM_CHAT_ID"), "text": text[:4000]},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        logging.warning(f"TELEGRAM_FAIL | {e}")
        return False


def _slack(subject: str, text: str) -> bool:
    try:
        r = httpx.post(os.getenv("SLACK_WEBHOOK_URL"), json={"text": f"*{subject}*\n{text}"}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logging.warning(f"SLACK_FAIL | {e}")
        return False


def _email(subject: str, text: str) -> bool:
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.getenv("ALERT_EMAIL_FROM") or os.getenv("SMTP_USER") or "rivaliq@localhost"
        msg["To"] = os.getenv("ALERT_EMAIL_TO")
        msg.set_content(text)
        host = os.getenv("SMTP_HOST")
        port = int(os.getenv("SMTP_PORT", "587"))
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        with server:
            if os.getenv("SMTP_USER"):
                server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
            server.send_message(msg)
        return True
    except Exception as e:
        logging.warning(f"EMAIL_FAIL | {e}")
        return False


_SENDERS = {"telegram": _telegram, "slack": _slack, "email": _email}


def send_text(subject: str, text: str, channels: list[str] | None = None) -> list[str]:
    """Send to the given channels (default: all). Returns the channels that succeeded."""
    delivered = []
    for ch in (ALL_CHANNELS if channels is None else channels):
        if is_configured(ch) and _SENDERS[ch](subject, text):
            delivered.append(ch)
    return delivered


def send_alert(change: dict) -> list[str]:
    """Immediate alert routed by severity tier. Returns channels that succeeded."""
    subject, body = format_alert(change)
    return send_text(subject, body, TIERS.get(change.get("severity", "low"), []))