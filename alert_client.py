"""Email alerts on fills and circuit-breaker/drawdown triggers.

Fully optional and fail-safe: if ALERT_EMAIL_TO or the SMTP credentials are
not configured, send() silently no-ops so the bot runs exactly as before.
A failed send is caught and swallowed (returns False) -- a trading bot must
never crash or block a cycle because email was down. Configure via env/
secrets; use an app-password, never a real account password.
"""
import smtplib
import ssl
from email.message import EmailMessage
import config


def is_configured() -> bool:
    return bool(config.ALERT_EMAIL_TO and config.ALERT_SMTP_USER and config.ALERT_SMTP_PASSWORD)


def send(subject: str, body: str) -> bool:
    """Send one alert email. Returns True on success, False if unconfigured
    or if the send failed (never raises)."""
    if not is_configured():
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = f"[trading-bot] {subject}"
        msg["From"] = config.ALERT_SMTP_USER
        msg["To"] = config.ALERT_EMAIL_TO
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(config.ALERT_SMTP_HOST, config.ALERT_SMTP_PORT, timeout=15) as s:
            s.starttls(context=ctx)
            s.login(config.ALERT_SMTP_USER, config.ALERT_SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception:
        return False
