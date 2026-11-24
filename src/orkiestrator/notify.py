"""Owner notifications via Discord, Telegram or SMTP. Silent no-op when nothing is configured.

A notification only ever tells the owner something is waiting and links to the panel. It carries no
approve button or token: approving happens only inside the panel session.
"""
import json
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage


def configured():
    if os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("CHANNEL_ORKIESTRATOR", "0") not in ("", "0"):
        return "discord"
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        return "telegram"
    if os.environ.get("SMTP_HOST") and os.environ.get("SMTP_TO"):
        return "smtp"
    return None


def _discord(text, http=None):
    url = f"https://discord.com/api/v10/channels/{os.environ['CHANNEL_ORKIESTRATOR']}/messages"
    data = json.dumps({"content": text[:1900]}).encode("utf-8")
    headers = {"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}", "Content-Type": "application/json",
               "User-Agent": "DiscordBot (https://github.com/RG-automations/orkiestrator, 0.1)"}
    (http or (lambda u, d: urllib.request.urlopen(urllib.request.Request(u, data=d, headers=headers), timeout=20).read()))(url, data)


def _telegram(text, http=None):
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}).encode()
    (http or (lambda u, d: urllib.request.urlopen(urllib.request.Request(u, data=d), timeout=20).read()))(url, data)


def _smtp(text, smtp_factory=None):
    msg = EmailMessage()
    msg["Subject"] = "Orkiestrator"
    msg["From"] = os.environ.get("SMTP_FROM", os.environ["SMTP_TO"])
    msg["To"] = os.environ["SMTP_TO"]
    msg.set_content(text)
    factory = smtp_factory or (lambda: smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20))
    with factory() as s:
        if not smtp_factory:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)


def send(text, panel_url="", http=None, smtp_factory=None):
    """Returns the channel used, or None. Never raises: a failed notification must not break the app."""
    channel = configured()
    if channel is None:
        return None
    body = text + (f"\n\nPanel: {panel_url}" if panel_url else "")
    try:
        if channel == "discord":
            _discord(body, http)
        elif channel == "telegram":
            _telegram(body, http)
        else:
            _smtp(body, smtp_factory)
    except Exception as e:  # noqa: BLE001
        print(f"[notify] failed: {e}")
        return None
    return channel
