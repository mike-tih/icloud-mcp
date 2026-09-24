"""Shared IMAP/SMTP primitives and MIME helpers (ported from the Resonar agent).

Highlights:
- ``extract_message_body`` prefers text/plain, falls back to text/html, renders
  HTML to readable text (inscriptis) and caps the size so a marketing newsletter
  cannot blow up the model context.
- Attachment discovery / selection helpers for ``email_get_attachment``.
- Special folder discovery (Sent / Trash) via IMAP SPECIAL-USE flags with
  iCloud-specific name fallbacks.
"""

import email
import email.message
import logging
import mimetypes
import os
import re
import smtplib
from dataclasses import asdict, dataclass
from email import encoders
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, getaddresses, parseaddr
from typing import Any

from imapclient import IMAPClient

from .config import config

logger = logging.getLogger(__name__)

BODY_KEYS = (b"BODY[]", "BODY[]", b"RFC822", "RFC822", b"BODY.PEEK[]")
SENT_CANDIDATES = ("Sent Messages", "Sent", "Sent Items", "Sent Mail")
TRASH_CANDIDATES = ("Deleted Messages", "Trash", "Deleted Items", "Bin")
DRAFTS_CANDIDATES = ("Drafts", "Draft")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


def get_imap_client(username: str, password: str) -> IMAPClient:
    client = IMAPClient(config.IMAP_SERVER, port=config.IMAP_PORT, ssl=True, use_uid=True, timeout=60)
    try:
        client.login(username, password)
    except Exception as e:
        try:
            client.shutdown()
        except Exception:
            pass
        raise PermissionError(
            f"IMAP login failed for {username}: {e}. iCloud Mail requires an app-specific "
            "password and an active iCloud Mail address (@icloud.com / @me.com / @mac.com). "
            "Apple IDs created with a third-party email cannot use iCloud Mail."
        ) from e
    return client


def close_imap_client(client: IMAPClient | None) -> None:
    if client is None:
        return
    try:
        client.logout()
    except Exception:
        try:
            client.shutdown()
        except Exception:
            pass


def get_smtp_client(username: str, password: str) -> smtplib.SMTP:
    client = smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=60)
    client.starttls()
    try:
        client.login(username, password)
    except smtplib.SMTPAuthenticationError as e:
        client.quit()
        raise PermissionError(
            f"SMTP login failed for {username}: {e}. Use an app-specific password."
        ) from e
    return client


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


def _folder_names(client: IMAPClient) -> list[str]:
    try:
        return [name for _flags, _delim, name in client.list_folders()]
    except Exception:
        return []


def find_special_folder(client: IMAPClient, flag: bytes, preferred: str, candidates: tuple[str, ...]) -> str | None:
    """Locate a special-use folder (``\\Sent``, ``\\Trash``) by flag, then by name."""
    try:
        found = client.find_special_folder(flag)
        if found:
            return found
    except Exception:
        pass
    names = _folder_names(client)
    lowered = {name.lower(): name for name in names}
    for candidate in (preferred, *candidates):
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def append_to_sent(client: IMAPClient, raw_message: bytes) -> str | None:
    """Store a copy of an outgoing message in the Sent folder. Returns the folder used."""
    folder = find_special_folder(client, b"\\Sent", config.SENT_FOLDER, SENT_CANDIDATES)
    attempts = [folder] if folder else []
    attempts += [name for name in (config.SENT_FOLDER, *SENT_CANDIDATES) if name not in attempts]
    for name in attempts:
        try:
            client.append(name, raw_message, flags=["\\Seen"])
            return name
        except Exception as e:
            logger.debug("Append to %s failed: %s", name, e)
    logger.warning("Could not save message copy to any Sent folder")
    return None


def append_to_drafts(client: IMAPClient, raw_message: bytes) -> str:
    """Store a message in the Drafts folder with the \\Draft flag. Returns the folder used."""
    folder = find_special_folder(client, b"\\Drafts", config.DRAFTS_FOLDER, DRAFTS_CANDIDATES)
    if not folder:
        raise ValueError("Could not find the Drafts folder (see email_list_folders); set DRAFTS_FOLDER")
    client.append(folder, raw_message, flags=["\\Draft", "\\Seen"])
    return folder


def move_messages(client: IMAPClient, uids: list[int], to_folder: str) -> None:
    """MOVE when the server supports it, else COPY + delete + UID EXPUNGE."""
    if client.has_capability("MOVE"):
        client.move(uids, to_folder)
        return
    client.copy(uids, to_folder)
    client.delete_messages(uids)
    _expunge(client, uids)


def _expunge(client: IMAPClient, uids: list[int]) -> None:
    if client.has_capability("UIDPLUS"):
        client.uid_expunge(uids)
    else:
        client.expunge()


def permanently_delete(client: IMAPClient, uids: list[int]) -> None:
    client.delete_messages(uids)
    _expunge(client, uids)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def decode_mime_header(value: Any) -> str:
    if not value:
        return ""
    parts = []
    for content, charset in decode_header(str(value)):
        if isinstance(content, bytes):
            try:
                parts.append(content.decode(charset or "utf-8", errors="ignore"))
            except Exception:
                parts.append(content.decode("utf-8", errors="ignore"))
        else:
            parts.append(str(content))
    return "".join(parts).replace("\r", "").replace("\n", " ").strip()


def flags_from_data(data: dict[Any, Any]) -> list[str]:
    raw = data.get(b"FLAGS") or data.get("FLAGS") or []
    return [flag.decode() if isinstance(flag, bytes) else str(flag) for flag in raw]


def raw_message_from_data(data: dict[Any, Any]) -> bytes | None:
    for key in BODY_KEYS:
        if key in data:
            return data[key]
    return None


def _decode_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="ignore")
    except LookupError:
        return payload.decode("utf-8", errors="ignore")


def _looks_like_html(body: str) -> bool:
    if not body:
        return False
    head = body.lstrip()[:200].lower()
    if head.startswith(("<!doctype", "<html", "<")):
        return True
    return body.count("<") > 20 and body.count(">") > 20


def truncate_body(body: str, max_chars: int | None = None) -> str:
    limit = max_chars or config.EMAIL_BODY_MAX_CHARS
    if not body:
        return ""
    body = body.strip()
    if limit and len(body) > limit:
        original = len(body)
        body = body[:limit].rstrip() + f"\n\n[... truncated, original was {original} chars ...]"
    return body


def normalize_email_body(body: str, max_chars: int | None = None) -> str:
    """HTML (even when disguised as text/plain) becomes readable text; size is capped."""
    if not body:
        return ""
    if _looks_like_html(body):
        try:
            from inscriptis import get_text

            body = get_text(body)
        except Exception:
            pass
    return truncate_body(body, max_chars)


def extract_message_body(msg: email.message.Message, prefer_html: bool = False) -> tuple[str, str]:
    """Return ``(body_text, body_html)``.

    ``body_text`` always: text/plain if present, otherwise text/html rendered to
    text. ``body_html`` is filled only when ``prefer_html`` is set (raw HTML,
    size-capped, not flattened).
    """
    plain = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and not plain:
                plain = _decode_payload(part)
            elif ctype == "text/html" and not html:
                html = _decode_payload(part)
    else:
        ctype = msg.get_content_type()
        content = _decode_payload(msg)
        if ctype == "text/html":
            html = content
        else:
            plain = content

    body_text = normalize_email_body(plain or html)
    body_html = truncate_body(html) if prefer_html else ""
    return body_text, body_html


def message_summary(
    uid: Any, msg: email.message.Message, data: dict[Any, Any], folder: str, include_body: bool
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": str(uid),
        "subject": decode_mime_header(msg.get("Subject", "")),
        "from": decode_mime_header(msg.get("From", "")),
        "to": decode_mime_header(msg.get("To", "")),
        "date": msg.get("Date", ""),
        "flags": flags_from_data(data),
        "unread": "\\Seen" not in flags_from_data(data),
        "folder": folder,
        "has_attachments": bool(list_attachments(msg)),
    }
    if include_body:
        item["body_text"], _ = extract_message_body(msg)
    return item


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@dataclass
class AttachmentMeta:
    index: int
    name: str
    mime_type: str
    size: int
    inline: bool


def _attachment_parts(msg: email.message.Message) -> list[tuple[AttachmentMeta, email.message.Message]]:
    parts = []
    index = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = (part.get("Content-Disposition") or "").lower()
        if not filename and "attachment" not in disposition:
            continue
        index += 1
        payload = part.get_payload(decode=True) or b""
        name = decode_mime_header(filename) if filename else f"attachment_{index}"
        parts.append(
            (
                AttachmentMeta(
                    index=index,
                    name=name,
                    mime_type=part.get_content_type(),
                    size=len(payload),
                    inline="inline" in disposition,
                ),
                part,
            )
        )
    return parts


def list_attachments(msg: email.message.Message) -> list[dict[str, Any]]:
    return [asdict(meta) for meta, _ in _attachment_parts(msg)]


def format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def find_attachment(msg: email.message.Message, ref: str) -> tuple[AttachmentMeta, bytes]:
    """Pick an attachment by 1-based index or (partial) file name."""
    parts = _attachment_parts(msg)
    if not parts:
        raise ValueError("This email has no attachments.")

    wanted = (ref or "").strip()
    chosen = None
    if wanted.isdigit():
        chosen = next((p for p in parts if p[0].index == int(wanted)), None)
    if chosen is None:
        lowered = wanted.lower()
        chosen = next((p for p in parts if p[0].name.lower() == lowered), None)
    if chosen is None and wanted:
        lowered = wanted.lower()
        chosen = next((p for p in parts if lowered in p[0].name.lower()), None)
    if chosen is None:
        available = "; ".join(
            f"{m.index}: {m.name} ({m.mime_type}, {format_size(m.size)})" for m, _ in parts
        )
        raise ValueError(
            f"No attachment matching '{ref}'. Available: {available}. Pass the index or the file name."
        )
    meta, part = chosen
    return meta, part.get_payload(decode=True) or b""


def fetch_message(client: IMAPClient, folder: str, message_id: str) -> tuple[email.message.Message, dict[Any, Any]]:
    """SELECT + FETCH one message by UID. Raises ValueError when not found."""
    client.select_folder(folder, readonly=True)
    try:
        uid = int(message_id)
    except ValueError as e:
        raise ValueError(f"message_id must be a numeric IMAP UID, got {message_id!r}") from e
    response = client.fetch([uid], [b"FLAGS", b"BODY.PEEK[]"])
    if uid not in response:
        raise ValueError(f"Message {message_id} not found in folder '{folder}'")
    data = response[uid]
    raw = raw_message_from_data(data)
    if raw is None:
        raise ValueError(f"Message {message_id} has no body. Keys: {list(data.keys())}")
    return email.message_from_bytes(raw), data


# ---------------------------------------------------------------------------
# Outgoing messages
# ---------------------------------------------------------------------------


def ensure_local_files_allowed() -> None:
    if config.LOCAL_FILES is False:
        raise PermissionError(
            "Local file access is disabled on this server (ICLOUD_MCP_LOCAL_FILES=false). "
            "Attachments can only be saved or attached when the MCP server runs on your "
            "own machine (stdio transport) or when the operator enables it."
        )


def resolve_local_path(path: str, must_exist: bool) -> str:
    """Expand and validate a user-supplied local path against LOCAL_FILES_ROOT."""
    ensure_local_files_allowed()
    resolved = os.path.realpath(os.path.expanduser(os.path.expandvars(path)))
    root = config.LOCAL_FILES_ROOT
    if root:
        root_real = os.path.realpath(os.path.expanduser(root))
        if os.path.commonpath([root_real, resolved]) != root_real:
            raise PermissionError(f"Path {path!r} is outside the allowed directory {root!r}")
    if must_exist and not os.path.isfile(resolved):
        raise FileNotFoundError(f"File not found: {path}")
    return resolved


def build_attachment_parts(paths: list[str]) -> list[MIMEBase]:
    parts: list[MIMEBase] = []
    total = 0
    for path in paths:
        resolved = resolve_local_path(path, must_exist=True)
        size = os.path.getsize(resolved)
        total += size
        if total > config.EMAIL_MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"Attachments exceed the {config.EMAIL_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit; "
                "send fewer or smaller files."
            )
        mime_type, _ = mimetypes.guess_type(resolved)
        maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
        part = MIMEBase(maintype, subtype)
        with open(resolved, "rb") as fh:
            part.set_payload(fh.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=os.path.basename(resolved))
        parts.append(part)
    return parts


def build_email_message(body: str, html: bool, attachment_parts: list[MIMEBase]) -> email.message.Message:
    """Plain or HTML body (HTML gets a text/plain alternative), wrapped in multipart/mixed if needed."""
    if html:
        body_part: email.message.Message = MIMEMultipart("alternative")
        body_part.attach(MIMEText(normalize_email_body(body, max_chars=0), "plain", "utf-8"))
        body_part.attach(MIMEText(body, "html", "utf-8"))
    else:
        body_part = MIMEText(body, "plain", "utf-8")

    if not attachment_parts:
        return body_part
    msg = MIMEMultipart("mixed")
    msg.attach(body_part)
    for part in attachment_parts:
        msg.attach(part)
    return msg


_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z][a-zA-Z0-9-]*(\s[^<>]*)?/?\s*>")
_ADDRESS_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")


def validate_header_text(value: str | None, name: str) -> str:
    """Reject CR/LF (header injection) in a free-text header such as Subject."""
    if value is None:
        return ""
    if "\r" in value or "\n" in value:
        raise ValueError(f"{name} must not contain line breaks")
    return value


def parse_recipients(value: str | None, name: str = "recipient") -> list[str]:
    """Parse a comma/semicolon separated recipient list into validated ``Name <addr>`` strings.

    Raises ValueError on header injection attempts or malformed addresses.
    """
    if not value:
        return []
    validate_header_text(value, name)
    result = []
    for display, addr in getaddresses([value.replace(";", ",")]):
        addr = addr.strip()
        if not addr:
            continue
        if not _ADDRESS_RE.match(addr):
            raise ValueError(f"Invalid email address in {name}: {addr!r}")
        display = display.strip()
        result.append(formataddr((display, addr)) if display else addr)
    if not result:
        raise ValueError(f"No valid email address found in {name}: {value!r}")
    return result


def bare_addresses(recipients: list[str]) -> list[str]:
    """SMTP envelope addresses for a list produced by parse_recipients."""
    return [parseaddr(r)[1] for r in recipients]


def split_addresses(value: str | None) -> list[str]:
    if not value:
        return []
    return [addr.strip() for addr in value.replace(";", ",").split(",") if addr.strip()]


def clean_rich_text(value: Any) -> str:
    """Normalise rich text stored in iCloud fields (event notes/location, contact notes).

    iCloud passes through HTML that users paste into Calendar/Contacts. ICLOUD_HTML_MODE:
    ``markdown`` (default) keeps links/emphasis/lists as Markdown, ``text`` renders to plain
    text via inscriptis, ``raw`` returns the value untouched.
    """
    if value is None:
        return ""
    text = str(value)
    mode = config.HTML_MODE
    if mode == "raw" or not _TAG_RE.search(text):
        return text
    if mode in ("text", "strip"):
        try:
            from inscriptis import get_text

            return get_text(text).strip()
        except Exception:
            return text
    from .html_render import render

    return render(text, mode="markdown") or ""
