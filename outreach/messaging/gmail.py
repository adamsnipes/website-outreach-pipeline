"""Gmail transport via the official Gmail API and OAuth2.

Never automates gmail.com in a browser.  Never asks for a password.  The token
lives outside the repository with restrictive permissions.

The transport is deliberately paranoid about *ambiguous* failures: if a send
times out or the connection drops after Gmail may already have accepted the
message, we do not retry blindly.  Every outgoing message carries a client-side
idempotency key in a custom header; on an ambiguous failure we search the
mailbox for that key and either adopt the message that was actually sent or
park the row in ``NEEDS_REVIEW``.  A duplicate cold email is worse than a
delayed one.
"""

from __future__ import annotations

import base64
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any

from outreach.config import Settings
from outreach.logging import get_logger

log = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

#: Custom header carrying our idempotency key so an ambiguous send can be
#: reconciled against the mailbox afterwards.
IDEMPOTENCY_HEADER = "X-StoopCraft-Idempotency-Key"
CAMPAIGN_HEADER = "X-StoopCraft-Campaign"


class GmailError(RuntimeError):
    """Gmail transport failure."""


class GmailAmbiguousSend(GmailError):
    """The send may or may not have gone through.  Never retry blindly."""


@dataclass
class GmailStatus:
    authenticated: bool
    reason: str
    email_address: str | None = None
    scopes: list[str] = field(default_factory=list)
    token_path: str | None = None
    expiry: datetime | None = None
    libraries_installed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "reason": self.reason,
            "email_address": self.email_address,
            "scopes": self.scopes,
            "token_path": self.token_path,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "libraries_installed": self.libraries_installed,
        }


@dataclass
class SendReceipt:
    sent: bool
    message_id: str | None = None
    thread_id: str | None = None
    idempotency_key: str | None = None
    ambiguous: bool = False
    error: str | None = None


@dataclass
class InboundMessage:
    """A message read back from the mailbox."""

    message_id: str
    thread_id: str
    from_email: str
    subject: str
    body_text: str
    internal_date: datetime | None = None
    label_ids: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)


def new_idempotency_key() -> str:
    return f"sc-{uuid.uuid4().hex}"


def build_mime(
    *,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str | None,
    sender_name: str,
    sender_email: str,
    reply_to: str | None = None,
    idempotency_key: str | None = None,
    campaign_label: str | None = None,
    unsubscribe_mailto: str | None = None,
) -> EmailMessage:
    """Build the outgoing MIME message.

    Includes ``List-Unsubscribe`` so mail clients can offer a one-click opt-out
    in addition to the plain-English instruction in the body.
    """
    message = EmailMessage()
    message["To"] = to_email
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    if reply_to:
        message["Reply-To"] = reply_to
    if idempotency_key:
        message[IDEMPOTENCY_HEADER] = idempotency_key
    if campaign_label:
        message[CAMPAIGN_HEADER] = campaign_label
    unsubscribe_target = unsubscribe_mailto or reply_to or sender_email
    if unsubscribe_target:
        message["List-Unsubscribe"] = f"<mailto:{unsubscribe_target}?subject=unsubscribe>"

    message.set_content(body_text)
    if body_html:
        message.add_alternative(body_html, subtype="html")
    return message


def mime_to_raw(message: EmailMessage) -> str:
    """URL-safe base64 encoding as the Gmail API expects."""
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class GmailClient:
    """Thin wrapper over the Gmail API with an injectable service for tests."""

    def __init__(self, settings: Settings, service: Any = None) -> None:
        self.settings = settings
        self._service = service
        self._credentials: Any = None

    # -- credentials ---------------------------------------------------
    @property
    def token_path(self) -> Path:
        return self.settings.gmail_token_file

    def _load_credentials(self) -> Any:
        if self._credentials is not None:
            return self._credentials
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise GmailError(
                "Gmail support needs the google auth libraries: "
                "pip install '.[gmail]'"
            ) from exc

        if not self.token_path.is_file():
            raise GmailError(
                f"No Gmail token at {self.token_path}. Run `python -m outreach gmail auth` first."
            )
        credentials = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self._write_token(credentials)
        if not credentials or not credentials.valid:
            raise GmailError("Stored Gmail credentials are invalid; re-run `gmail auth`.")
        self._credentials = credentials
        return credentials

    def _write_token(self, credentials: Any) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(credentials.to_json(), encoding="utf-8")
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            log.warning("could not restrict permissions on the Gmail token file")

    def authorize(self, *, open_browser: bool = True) -> GmailStatus:
        """Run the installed-app OAuth flow and persist the token."""
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise GmailError(
                "Gmail OAuth needs google-auth-oauthlib: pip install '.[gmail]'"
            ) from exc

        secret_path = Path(self.settings.gmail_client_secret_path or "").expanduser()
        if not self.settings.gmail_client_secret_path or not secret_path.is_file():
            raise GmailError(
                "GMAIL_CLIENT_SECRET_PATH must point at the OAuth client secret JSON "
                "downloaded from Google Cloud Console (Desktop app credentials)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
        credentials = (
            flow.run_local_server(port=0, open_browser=open_browser)
            if open_browser
            else flow.run_console()
        )
        self._write_token(credentials)
        self._credentials = credentials
        self._service = None
        log.info("gmail authorised", extra={"token_path": str(self.token_path)})
        return self.status()

    # -- service -------------------------------------------------------
    def service(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise GmailError(
                "Gmail support needs google-api-python-client: pip install '.[gmail]'"
            ) from exc
        self._service = build(
            "gmail", "v1", credentials=self._load_credentials(), cache_discovery=False
        )
        return self._service

    def status(self) -> GmailStatus:
        """Non-destructive authentication check."""
        try:
            import google.oauth2.credentials  # noqa: F401
            import googleapiclient  # noqa: F401
        except ImportError as exc:
            return GmailStatus(
                False, f"google client libraries are not installed ({exc})",
                token_path=str(self.token_path), libraries_installed=False,
            )
        if not self.token_path.is_file():
            return GmailStatus(
                False,
                f"no OAuth token at {self.token_path}; run `python -m outreach gmail auth`",
                token_path=str(self.token_path),
            )
        try:
            credentials = self._load_credentials()
        except GmailError as exc:
            return GmailStatus(False, str(exc), token_path=str(self.token_path))
        try:
            profile = self.service().users().getProfile(userId="me").execute()
            address = profile.get("emailAddress")
        except Exception as exc:  # pragma: no cover - network dependent
            return GmailStatus(
                False, f"token present but Gmail rejected it: {exc}",
                token_path=str(self.token_path),
            )
        return GmailStatus(
            True,
            "authenticated",
            email_address=address,
            scopes=list(getattr(credentials, "scopes", []) or []),
            token_path=str(self.token_path),
            expiry=getattr(credentials, "expiry", None),
        )

    # -- sending -------------------------------------------------------
    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        """Look in Sent for a message carrying our idempotency key.

        This is what makes an ambiguous failure recoverable instead of a
        coin flip between "never sent" and "sent twice".
        """
        try:
            response = (
                self.service()
                .users()
                .messages()
                .list(userId="me", q=f'"{key}"', labelIds=["SENT"], maxResults=5)
                .execute()
            )
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("idempotency lookup failed", extra={"error": str(exc)})
            return None
        for entry in response.get("messages", []) or []:
            try:
                full = (
                    self.service()
                    .users()
                    .messages()
                    .get(userId="me", id=entry["id"], format="metadata",
                         metadataHeaders=[IDEMPOTENCY_HEADER])
                    .execute()
                )
            except Exception:  # pragma: no cover - network dependent
                continue
            headers = {
                h.get("name", "").lower(): h.get("value", "")
                for h in (full.get("payload", {}).get("headers") or [])
            }
            if headers.get(IDEMPOTENCY_HEADER.lower()) == key:
                return {"id": full.get("id"), "threadId": full.get("threadId")}
        return None

    def send(
        self,
        *,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: str | None,
        idempotency_key: str,
        campaign_label: str | None = None,
        dry_run: bool = False,
    ) -> SendReceipt:
        """Transmit one message.  Refuses outright during a dry run."""
        if dry_run:
            # Defence in depth: the pipeline already blocks this path, and the
            # transport itself refuses too.
            raise GmailError("DRY RUN: GmailClient.send must never transmit")

        sender_email = self.settings.sender_email or ""
        if not sender_email:
            status = self.status()
            if not status.authenticated or not status.email_address:
                return SendReceipt(False, error=status.reason)
            sender_email = status.email_address

        message = build_mime(
            to_email=to_email,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            sender_name=self.settings.sender_name,
            sender_email=sender_email,
            reply_to=self.settings.sender_reply_to or None,
            idempotency_key=idempotency_key,
            campaign_label=campaign_label,
        )
        payload = {"raw": mime_to_raw(message)}
        try:
            result = self.service().users().messages().send(userId="me", body=payload).execute()
        except Exception as exc:
            # Anything that could have landed *after* Gmail accepted it is
            # ambiguous. Reconcile against the mailbox rather than retrying.
            log.warning(
                "gmail send raised; reconciling against the mailbox",
                extra={"error": str(exc), "idempotency_key": idempotency_key},
            )
            found = self.find_by_idempotency_key(idempotency_key)
            if found:
                return SendReceipt(
                    True,
                    message_id=found.get("id"),
                    thread_id=found.get("threadId"),
                    idempotency_key=idempotency_key,
                    error=f"send raised {exc} but the message is in Sent",
                )
            if _is_definite_failure(exc):
                return SendReceipt(False, idempotency_key=idempotency_key, error=str(exc))
            return SendReceipt(
                False, idempotency_key=idempotency_key, ambiguous=True, error=str(exc)
            )

        return SendReceipt(
            True,
            message_id=result.get("id"),
            thread_id=result.get("threadId"),
            idempotency_key=idempotency_key,
        )

    # -- reading -------------------------------------------------------
    def list_thread_messages(self, thread_id: str) -> list[InboundMessage]:
        try:
            thread = self.service().users().threads().get(userId="me", id=thread_id, format="full").execute()
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("could not read thread", extra={"thread_id": thread_id, "error": str(exc)})
            return []
        return [parse_gmail_message(m) for m in thread.get("messages", []) or []]

    def search(self, query: str, *, limit: int = 25) -> list[InboundMessage]:
        try:
            response = (
                self.service()
                .users()
                .messages()
                .list(userId="me", q=query, maxResults=limit)
                .execute()
            )
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("gmail search failed", extra={"error": str(exc)})
            return []
        out: list[InboundMessage] = []
        for entry in response.get("messages", []) or []:
            try:
                full = (
                    self.service().users().messages()
                    .get(userId="me", id=entry["id"], format="full").execute()
                )
            except Exception:  # pragma: no cover - network dependent
                continue
            out.append(parse_gmail_message(full))
        return out


def _is_definite_failure(exc: Exception) -> bool:
    """Errors where we know for certain nothing was sent."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in (400, 401, 403, 404):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in ("invalid to header", "invalid recipient", "malformed", "unauthorized")
    )


def parse_gmail_message(payload: dict[str, Any]) -> InboundMessage:
    """Flatten a Gmail API message resource into something usable."""
    headers = {
        str(h.get("name", "")).lower(): str(h.get("value", ""))
        for h in (payload.get("payload", {}).get("headers") or [])
    }
    _, from_email = parseaddr(headers.get("from", ""))
    internal: datetime | None = None
    raw_date = payload.get("internalDate")
    if raw_date:
        try:
            internal = datetime.fromtimestamp(int(raw_date) / 1000, tz=UTC)
        except (TypeError, ValueError):
            internal = None
    return InboundMessage(
        message_id=str(payload.get("id") or ""),
        thread_id=str(payload.get("threadId") or ""),
        from_email=from_email or headers.get("from", ""),
        subject=headers.get("subject", ""),
        body_text=extract_plain_body(payload.get("payload") or {}),
        internal_date=internal,
        label_ids=list(payload.get("labelIds") or []),
        headers=headers,
    )


def extract_plain_body(part: dict[str, Any], depth: int = 0) -> str:
    """Depth-first search for the best text body in a MIME tree."""
    if depth > 8:  # pragma: no cover - pathological nesting
        return ""
    mime_type = str(part.get("mimeType") or "")
    body = part.get("body") or {}
    data = body.get("data")
    if data and mime_type.startswith("text/plain"):
        return _decode(data)
    collected: list[str] = []
    for child in part.get("parts") or []:
        text = extract_plain_body(child, depth + 1)
        if text:
            collected.append(text)
    if collected:
        return "\n".join(collected)
    if data and mime_type.startswith("text/html"):
        return re.sub(r"<[^>]+>", " ", _decode(data))
    return ""


def _decode(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except (ValueError, TypeError):  # pragma: no cover - malformed payloads
        return ""
