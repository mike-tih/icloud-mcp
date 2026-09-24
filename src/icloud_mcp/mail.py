"""IMAP/SMTP operations for iCloud Mail."""

import email as email_lib
import logging
from datetime import date
from email.utils import formatdate
from typing import Any

from imapclient import IMAPClient

from .auth import require_auth
from .config import config
from .mail_utils import (
    TRASH_CANDIDATES,
    append_to_drafts,
    append_to_sent,
    bare_addresses,
    build_attachment_parts,
    build_email_message,
    check_recipients_allowed,
    close_imap_client,
    ensure_local_files_allowed,
    extract_message_body,
    fetch_message,
    find_attachment,
    find_special_folder,
    flags_from_data,
    get_imap_client,
    get_smtp_client,
    list_attachments,
    message_summary,
    move_messages,
    parse_recipients,
    permanently_delete,
    raw_message_from_data,
    resolve_local_path,
    validate_header_text,
)

logger = logging.getLogger(__name__)

# Backwards-compatible aliases used by calendar.py
_get_imap_client = get_imap_client
_close_imap_client = close_imap_client
_append_to_sent = append_to_sent


def _uid(message_id: str) -> int:
    try:
        return int(message_id)
    except (TypeError, ValueError) as e:
        raise ValueError(f"message_id must be a numeric IMAP UID, got {message_id!r}") from e


def _fetch_summaries(
    client: IMAPClient, uids: list[int], folder: str, include_body: bool
) -> list[dict[str, Any]]:
    if not uids:
        return []
    fields = [b"FLAGS", b"BODY.PEEK[]"] if include_body else [b"FLAGS", b"BODY.PEEK[HEADER]", b"BODYSTRUCTURE"]
    response = client.fetch(uids, fields)
    result = []
    for uid in uids:  # keep newest-first order
        data = response.get(uid)
        if not data:
            continue
        raw = raw_message_from_data(data)
        if raw is None:
            for key in (b"BODY[HEADER]", "BODY[HEADER]"):
                if key in data:
                    raw = data[key]
                    break
        if raw is None:
            continue
        try:
            msg = email_lib.message_from_bytes(raw)
            item = message_summary(uid, msg, data, folder, include_body)
            if not include_body:
                item["has_attachments"] = _bodystructure_has_attachments(data)
            result.append(item)
        except Exception as e:
            logger.debug("Skipping message %s: %s", uid, e)
    return result


def _bodystructure_has_attachments(data: dict[Any, Any]) -> bool:
    structure = data.get(b"BODYSTRUCTURE") or data.get("BODYSTRUCTURE")
    if structure is None:
        return False
    text = repr(structure).lower()
    return "attachment" in text or "filename" in text


def _newest(uids: list[int], limit: int) -> list[int]:
    uids = sorted(uids)
    if limit and len(uids) > limit:
        uids = uids[-limit:]
    uids.reverse()
    return uids


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def list_folders() -> list[dict[str, Any]]:
    username, password = require_auth()
    client = get_imap_client(username, password)
    try:
        result = []
        for flags, delimiter, name in client.list_folders():
            result.append(
                {
                    "name": name,
                    "flags": [f.decode() if isinstance(f, bytes) else str(f) for f in flags],
                    "delimiter": delimiter.decode() if isinstance(delimiter, bytes) else delimiter,
                }
            )
        return result
    finally:
        close_imap_client(client)


def list_messages(
    folder: str = "INBOX", limit: int = 50, unread_only: bool = False, include_body: bool = True
) -> list[dict[str, Any]]:
    username, password = require_auth()
    client = get_imap_client(username, password)
    try:
        client.select_folder(folder, readonly=True)
        uids = client.search(["UNSEEN"] if unread_only else ["ALL"])
        return _fetch_summaries(client, _newest(list(uids), limit), folder, include_body)
    finally:
        close_imap_client(client)


def _parse_date(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as e:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}") from e


def search_messages(
    query: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    since: str | None = None,
    before: str | None = None,
    unread_only: bool = False,
    folder: str = "INBOX",
    limit: int = 50,
    include_body: bool = True,
) -> list[dict[str, Any]]:
    if not any([query, sender, recipient, subject, body, since, before, unread_only]):
        raise ValueError(
            "Provide at least one filter: query, sender, recipient, subject, body, since, before or unread_only"
        )

    criteria: list[Any] = []
    if query:
        criteria.append(["OR", ["SUBJECT", query], ["FROM", query]])
    if sender:
        criteria.extend(["FROM", sender])
    if recipient:
        criteria.append(["OR", ["TO", recipient], ["CC", recipient]])
    if subject:
        criteria.extend(["SUBJECT", subject])
    if body:
        criteria.extend(["BODY", body])
    since_date = _parse_date(since, "since")
    before_date = _parse_date(before, "before")
    if since_date:
        criteria.extend(["SINCE", since_date])
    if before_date:
        criteria.extend(["BEFORE", before_date])
    if unread_only:
        criteria.append("UNSEEN")

    username, password = require_auth()
    client = get_imap_client(username, password)
    try:
        client.select_folder(folder, readonly=True)
        try:
            uids = list(client.search(criteria, charset="UTF-8"))
        except Exception as e:
            # Some servers reject CHARSET UTF-8; fall back to ASCII search when possible,
            # otherwise to a local scan of recent headers.
            logger.warning("UTF-8 IMAP search failed (%s); falling back", e)
            try:
                uids = list(client.search(criteria))
            except Exception:
                return _local_search(client, folder, query, sender, recipient, subject, limit, include_body)
        return _fetch_summaries(client, _newest(uids, limit), folder, include_body)
    finally:
        close_imap_client(client)


def _local_search(
    client: IMAPClient,
    folder: str,
    query: str | None,
    sender: str | None,
    recipient: str | None,
    subject: str | None,
    limit: int,
    include_body: bool,
) -> list[dict[str, Any]]:
    uids = _newest(list(client.search(["ALL"])), max(limit * 10, 200))
    candidates = _fetch_summaries(client, uids, folder, include_body=False)

    def matches(item: dict[str, Any]) -> bool:
        subj, frm, to = item["subject"].lower(), item["from"].lower(), item["to"].lower()
        if query and query.lower() not in subj and query.lower() not in frm:
            return False
        if sender and sender.lower() not in frm:
            return False
        if recipient and recipient.lower() not in to:
            return False
        if subject and subject.lower() not in subj:
            return False
        return True

    hits = [item for item in candidates if matches(item)][:limit]
    if include_body and hits:
        return _fetch_summaries(client, [int(h["id"]) for h in hits], folder, include_body=True)
    return hits


def _message_details(
    uid: int, data: dict[Any, Any], folder: str, include_body: bool, full_html: bool
) -> dict[str, Any]:
    raw = raw_message_from_data(data)
    if raw is None:
        raise ValueError(f"Message {uid} has no body. Keys: {list(data.keys())}")
    msg = email_lib.message_from_bytes(raw)
    from .mail_utils import decode_mime_header

    result: dict[str, Any] = {
        "id": str(uid),
        "subject": decode_mime_header(msg.get("Subject", "")),
        "from": decode_mime_header(msg.get("From", "")),
        "to": decode_mime_header(msg.get("To", "")),
        "cc": decode_mime_header(msg.get("Cc", "")),
        "reply_to": decode_mime_header(msg.get("Reply-To", "")),
        "date": msg.get("Date", ""),
        "message_id_header": msg.get("Message-ID", ""),
        "in_reply_to": msg.get("In-Reply-To", ""),
        "flags": flags_from_data(data),
        "unread": "\\Seen" not in flags_from_data(data),
        "folder": folder,
    }
    if include_body:
        body_text, body_html = extract_message_body(msg, prefer_html=full_html)
        result["body_text"] = body_text
        if full_html:
            result["body_html"] = body_html
    result["attachments"] = list_attachments(msg)
    return result


def get_message(
    message_id: str, folder: str = "INBOX", include_body: bool = True, full_html: bool = False
) -> dict[str, Any]:
    username, password = require_auth()
    client = get_imap_client(username, password)
    try:
        client.select_folder(folder, readonly=True)
        uid = _uid(message_id)
        response = client.fetch([uid], [b"FLAGS", b"BODY.PEEK[]"])
        if uid not in response:
            raise ValueError(f"Message {message_id} not found in folder '{folder}'")
        return _message_details(uid, response[uid], folder, include_body, full_html)
    finally:
        close_imap_client(client)


def get_messages(
    message_ids: list[str], folder: str = "INBOX", include_body: bool = True, full_html: bool = False
) -> list[dict[str, Any]]:
    username, password = require_auth()
    client = get_imap_client(username, password)
    try:
        client.select_folder(folder, readonly=True)
        uids = [_uid(m) for m in message_ids]
        response = client.fetch(uids, [b"FLAGS", b"BODY.PEEK[]"])
        results = []
        for uid in uids:
            if uid not in response:
                results.append({"id": str(uid), "error": f"Message {uid} not found in folder '{folder}'"})
                continue
            try:
                results.append(_message_details(uid, response[uid], folder, include_body, full_html))
            except Exception as e:
                results.append({"id": str(uid), "error": str(e)})
        return results
    finally:
        close_imap_client(client)


def get_attachment(
    message_id: str, attachment: str, folder: str = "INBOX", save_dir: str | None = None
) -> dict[str, Any]:
    """Return attachment metadata plus either a saved path or the raw bytes."""
    username, password = require_auth()
    client = get_imap_client(username, password)
    try:
        msg, _ = fetch_message(client, folder, message_id)
    finally:
        close_imap_client(client)

    meta, data = find_attachment(msg, attachment)
    result: dict[str, Any] = {
        "message_id": message_id,
        "name": meta.name,
        "mime_type": meta.mime_type,
        "size": len(data),
    }
    if save_dir:
        ensure_local_files_allowed()
        import os

        directory = resolve_local_path(save_dir, must_exist=False)
        os.makedirs(directory, exist_ok=True)
        safe_name = os.path.basename(meta.name).replace("/", "_").replace("\\", "_") or f"attachment_{meta.index}"
        target = os.path.join(directory, safe_name)
        base, ext = os.path.splitext(target)
        counter = 1
        while os.path.exists(target):
            target = f"{base} ({counter}){ext}"
            counter += 1
        with open(target, "wb") as fh:
            fh.write(data)
        result["saved_to"] = target
    else:
        result["data"] = data
    return result


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _compose(
    username: str,
    password: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None,
    bcc: str | None,
    html: bool,
    attachment_paths: list[str] | None,
    reply_to_message_id: str | None,
    reply_to_folder: str,
) -> tuple[Any, list[str], list[Any]]:
    """Build the MIME message; returns (message, envelope recipients, attachment parts)."""
    to_list = parse_recipients(to, "to")
    cc_list = parse_recipients(cc, "cc")
    bcc_list = parse_recipients(bcc, "bcc")
    subject = validate_header_text(subject, "subject")
    if not to_list:
        raise ValueError("At least one recipient is required in 'to'")

    parts = build_attachment_parts(attachment_paths) if attachment_paths else []
    msg = build_email_message(body, html, parts)
    msg["From"] = username
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    if reply_to_message_id:
        client = get_imap_client(username, password)
        try:
            original, _ = fetch_message(client, reply_to_folder, reply_to_message_id)
        finally:
            close_imap_client(client)
        original_id = original.get("Message-ID")
        if original_id:
            msg["In-Reply-To"] = original_id
            references = (original.get("References") or "").split()
            references.append(original_id)
            msg["References"] = " ".join(references)

    recipients = bare_addresses(to_list + cc_list + bcc_list)
    check_recipients_allowed(recipients)
    return msg, recipients, parts


def send_message(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html: bool = False,
    attachment_paths: list[str] | None = None,
    reply_to_message_id: str | None = None,
    reply_to_folder: str = "INBOX",
) -> dict[str, Any]:
    username, password = require_auth()
    msg, recipients, parts = _compose(
        username, password, to, subject, body, cc, bcc, html, attachment_paths, reply_to_message_id, reply_to_folder
    )

    with get_smtp_client(username, password) as smtp:
        smtp.send_message(msg, from_addr=username, to_addrs=recipients)

    saved_to = None
    try:
        client = get_imap_client(username, password)
        try:
            saved_to = append_to_sent(client, msg.as_bytes())
        finally:
            close_imap_client(client)
    except Exception as e:
        logger.warning("Could not save sent copy: %s", e)

    result: dict[str, Any] = {
        "status": "success",
        "message": f"Email sent to {', '.join(recipients)}",
        "attachments": [p.get_filename() for p in parts],
    }
    if saved_to:
        result["saved_to_folder"] = saved_to
    return result


def save_draft(
    to: str | None,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html: bool = False,
    attachment_paths: list[str] | None = None,
    reply_to_message_id: str | None = None,
    reply_to_folder: str = "INBOX",
) -> dict[str, Any]:
    """Store a message in Drafts without sending it (recipients may be empty)."""
    username, password = require_auth()
    to_list = parse_recipients(to, "to") if to else []
    cc_list = parse_recipients(cc, "cc") if cc else []
    bcc_list = parse_recipients(bcc, "bcc") if bcc else []
    subject = validate_header_text(subject, "subject")
    # A draft to a blocked address is one click away from a send in the mail client.
    check_recipients_allowed(bare_addresses(to_list + cc_list + bcc_list))

    parts = build_attachment_parts(attachment_paths) if attachment_paths else []
    msg = build_email_message(body, html, parts)
    msg["From"] = username
    if to_list:
        msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    if bcc_list:
        msg["Bcc"] = ", ".join(bcc_list)

    client = get_imap_client(username, password)
    try:
        if reply_to_message_id:
            original, _ = fetch_message(client, reply_to_folder, reply_to_message_id)
            original_id = original.get("Message-ID")
            if original_id:
                msg["In-Reply-To"] = original_id
                references = (original.get("References") or "").split()
                references.append(original_id)
                msg["References"] = " ".join(references)
        folder = append_to_drafts(client, msg.as_bytes())
    finally:
        close_imap_client(client)

    return {
        "status": "success",
        "message": f"Draft saved to {folder}",
        "folder": folder,
        "to": bare_addresses(to_list),
        "subject": subject,
        "attachments": [p.get_filename() for p in parts],
    }


def move_message(message_id: str, from_folder: str, to_folder: str) -> dict[str, str]:
    username, password = require_auth()
    client = get_imap_client(username, password)
    try:
        client.select_folder(from_folder)
        move_messages(client, [_uid(message_id)], to_folder)
        return {"status": "success", "message": f"Message {message_id} moved from {from_folder} to {to_folder}"}
    finally:
        close_imap_client(client)


def delete_message(message_id: str, folder: str = "INBOX", permanent: bool = False) -> dict[str, str]:
    username, password = require_auth()
    client = get_imap_client(username, password)
    try:
        client.select_folder(folder)
        uid = _uid(message_id)
        if permanent:
            permanently_delete(client, [uid])
            return {"status": "success", "message": f"Message {message_id} permanently deleted"}

        trash = find_special_folder(client, b"\\Trash", config.TRASH_FOLDER, TRASH_CANDIDATES)
        if not trash:
            raise ValueError(
                "Could not find the Trash folder. Pass permanent=true to delete permanently, "
                "or move the message with email_move."
            )
        if folder.lower() == trash.lower():
            permanently_delete(client, [uid])
            return {"status": "success", "message": f"Message {message_id} permanently deleted from {trash}"}
        move_messages(client, [uid], trash)
        return {"status": "success", "message": f"Message {message_id} moved to {trash}"}
    finally:
        close_imap_client(client)


def _set_seen(message_id: str, folder: str, seen: bool) -> dict[str, str]:
    username, password = require_auth()
    client = get_imap_client(username, password)
    try:
        client.select_folder(folder)
        uid = _uid(message_id)
        if seen:
            client.add_flags([uid], ["\\Seen"])
        else:
            client.remove_flags([uid], ["\\Seen"])
        state = "read" if seen else "unread"
        return {"status": "success", "message": f"Message {message_id} marked as {state}"}
    finally:
        close_imap_client(client)


def mark_as_read(message_id: str, folder: str = "INBOX") -> dict[str, str]:
    return _set_seen(message_id, folder, True)


def mark_as_unread(message_id: str, folder: str = "INBOX") -> dict[str, str]:
    return _set_seen(message_id, folder, False)
