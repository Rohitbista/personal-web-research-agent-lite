"""Brevo transactional email. Meant to run as FastAPI background tasks: never raises."""
from __future__ import annotations

import html
import logging
from urllib.parse import urlencode

import httpx

from personal_web_research_agent_lite.config.settings import settings

logger = logging.getLogger(__name__)

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


async def _send(client: httpx.AsyncClient, to_email: str, subject: str, body_html: str) -> None:
    response = await client.post(
        BREVO_SEND_URL,
        headers={"api-key": settings.brevo_api_key, "accept": "application/json"},
        json={
            "sender": {"email": settings.brevo_sender_email, "name": settings.brevo_sender_name},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": body_html,
        },
    )
    response.raise_for_status()


def _frontend_url(route: str, raw_token: str) -> str:
    """e.g. https://user.github.io + /repo  ->  https://user.github.io/repo/#/verify?token=...

    frontend_path is optional and may be given with or without slashes ("repo", "/repo/").
    The hash route keeps this working on GitHub Pages (no server-side routing needed).
    """
    base = settings.frontend_base_url.rstrip("/")
    path = (settings.frontend_path or "").strip().strip("/")
    if path:
        base = f"{base}/{path}"
    return f"{base}/#/{route}?{urlencode({'token': raw_token})}"


def _link_block(url: str, label: str) -> str:
    """Button-style link plus the plain URL as a fallback (the token is only inside the URL)."""
    safe = html.escape(url, quote=True)
    return (
        f'<p><a href="{safe}" style="display:inline-block;padding:10px 18px;background:#1b5e6b;'
        f'color:#ffffff;text-decoration:none;border-radius:6px">{html.escape(label)}</a></p>'
        f'<p style="font-size:13px;color:#555">Or copy this link into your browser:<br>{safe}</p>'
    )


async def send_verification_email(client: httpx.AsyncClient, to_email: str, raw_token: str) -> None:
    """Never raises (the user can hit 'resend')."""
    name = html.escape(settings.brevo_sender_name)

    if not settings.brevo_api_key:
        logger.warning("Brevo not configured. Verification token for %s: %s (link: %s)",
                       to_email, raw_token,
                       _frontend_url("verify", raw_token) if settings.frontend_base_url else "n/a")
        return

    if settings.frontend_base_url:
        action = _link_block(_frontend_url("verify", raw_token), "Verify your email")
    else:
        action = ("<p>Send this token to <code>POST /auth/verify-email</code>:</p>"
                  f"<p><code>{raw_token}</code></p>")
    body = (
        f"<p>Welcome to {name}!</p>{action}"
        f"<p>This link expires in {settings.verification_token_ttl_hours} hours. "
        "If you didn't sign up, you can ignore this email.</p>"
    )
    try:
        await _send(client, to_email, "Verify your email", body)
    except httpx.HTTPError as e:
        logger.exception("Brevo send failed for %s: %r", to_email, e)


async def send_password_reset_email(client: httpx.AsyncClient, to_email: str, raw_token: str) -> None:
    if not settings.brevo_api_key:
        logger.warning("Brevo not configured. Reset token for %s: %s (link: %s)",
                       to_email, raw_token,
                       _frontend_url("reset", raw_token) if settings.frontend_base_url else "n/a")
        return

    if settings.frontend_base_url:
        action = _link_block(_frontend_url("reset", raw_token), "Set a new password")
    else:
        action = ("<p>Send this token together with your new password to "
                  "<code>POST /auth/reset-password</code>:</p>"
                  f"<p><code>{raw_token}</code></p>")
    body = (
        "<p>We received a request to reset your password.</p>"
        f"{action}"
        f"<p>This link expires in {settings.password_reset_token_ttl_minutes} minutes. "
        "If you didn't ask for this, you can ignore this email.</p>"
    )
    try:
        await _send(client, to_email, "Reset your password", body)
    except httpx.HTTPError as e:
        logger.exception("Brevo send failed for %s: %r", to_email, e)