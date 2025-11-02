"""IMAP/SMTP tools for email management."""

import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from typing import List, Dict, Any, Optional
from datetime import datetime
from fastmcp import Context
from imapclient import IMAPClient
from .auth import require_auth
from .config import config


def _get_imap_client(username: str, password: str) -> IMAPClient:
    """Create IMAP client (stateless)."""
    client = IMAPClient(config.IMAP_SERVER, port=config.IMAP_PORT, ssl=True)
    client.login(username, password)
    return client


def _get_smtp_client(username: str, password: str) -> smtplib.SMTP:
    """Create SMTP client (stateless)."""
    client = smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT)
    client.starttls()
    client.login(username, password)
    return client


def _decode_mime_header(header_value: str) -> str:
    """Decode MIME encoded email header."""
    if not header_value:
        return ""

    decoded_parts = decode_header(header_value)
    result = []

    for content, charset in decoded_parts:
        if isinstance(content, bytes):
            try:
                result.append(content.decode(charset or 'utf-8', errors='ignore'))
            except:
                result.append(content.decode('utf-8', errors='ignore'))
        else:
            result.append(str(content))

    return ' '.join(result)


async def list_folders(context: Context) -> List[Dict[str, Any]]:
    """
    List all email folders/mailboxes.

    Returns:
        List of folders with name and flags
    """
    username, password = require_auth(context)

    with _get_imap_client(username, password) as client:
        folders = client.list_folders()

        result = []
        for flags, delimiter, name in folders:
            result.append({
                "name": name,
                "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in flags],
                "delimiter": delimiter
            })

        return result


async def list_messages(
    context: Context,
    folder: str = "INBOX",
    limit: int = 50,
    unread_only: bool = False
) -> List[Dict[str, Any]]:
    """
    List messages in a folder.

    Args:
        folder: Folder name (default: INBOX)
        limit: Maximum number of messages to return
        unread_only: Only return unread messages

    Returns:
        List of messages with basic info
    """
    username, password = require_auth(context)

    with _get_imap_client(username, password) as client:
        client.select_folder(folder)

        # Search for messages
        if unread_only:
            messages = client.search(['UNSEEN'])
        else:
            messages = client.search(['ALL'])

        # Get most recent messages
        message_ids = list(messages)[-limit:] if len(messages) > limit else list(messages)
        message_ids.reverse()  # Most recent first

        if not message_ids:
            return []

        # Fetch message headers
        response = client.fetch(message_ids, ['FLAGS', 'RFC822.HEADER'])

        result = []
        for msg_id, data in response.items():
            try:
                header_data = data[b'RFC822.HEADER']
                msg = email.message_from_bytes(header_data)

                result.append({
                    "id": str(msg_id),
                    "subject": _decode_mime_header(msg.get('Subject', '')),
                    "from": _decode_mime_header(msg.get('From', '')),
                    "to": _decode_mime_header(msg.get('To', '')),
                    "date": msg.get('Date', ''),
                    "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data[b'FLAGS']],
                    "folder": folder
                })
            except Exception as e:
                continue

        return result


async def get_message(
    context: Context,
    message_id: str,
    folder: str = "INBOX",
    include_body: bool = True
) -> Dict[str, Any]:
    """
    Get a specific message with full details.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)
        include_body: Include message body content

    Returns:
        Complete message details
    """
    username, password = require_auth(context)

    with _get_imap_client(username, password) as client:
        client.select_folder(folder)

        msg_id = int(message_id)
        response = client.fetch([msg_id], ['FLAGS', 'RFC822'])

        if msg_id not in response:
            raise ValueError(f"Message {message_id} not found")

        data = response[msg_id]
        raw_email = data[b'RFC822']
        msg = email.message_from_bytes(raw_email)

        result = {
            "id": message_id,
            "subject": _decode_mime_header(msg.get('Subject', '')),
            "from": _decode_mime_header(msg.get('From', '')),
            "to": _decode_mime_header(msg.get('To', '')),
            "cc": _decode_mime_header(msg.get('Cc', '')),
            "date": msg.get('Date', ''),
            "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data[b'FLAGS']],
            "folder": folder
        }

        if include_body:
            # Extract body
            body_text = ""
            body_html = ""

            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    if content_type == "text/plain":
                        try:
                            body_text = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except:
                            pass
                    elif content_type == "text/html":
                        try:
                            body_html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except:
                            pass
            else:
                try:
                    body_text = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                except:
                    pass

            result["body_text"] = body_text
            result["body_html"] = body_html

        return result


async def search_messages(
    context: Context,
    query: str,
    folder: str = "INBOX",
    limit: int = 50
) -> List[Dict[str, Any]]:
    """
    Search for messages by text query.

    Args:
        query: Search text (searches subject and from fields)
        folder: Folder name (default: INBOX)
        limit: Maximum number of results

    Returns:
        List of matching messages
    """
    username, password = require_auth(context)

    with _get_imap_client(username, password) as client:
        client.select_folder(folder)

        # Search by subject or from
        messages = client.search([
            'OR',
            ['SUBJECT', query],
            ['FROM', query]
        ])

        message_ids = list(messages)[-limit:] if len(messages) > limit else list(messages)
        message_ids.reverse()

        if not message_ids:
            return []

        response = client.fetch(message_ids, ['FLAGS', 'RFC822.HEADER'])

        result = []
        for msg_id, data in response.items():
            try:
                header_data = data[b'RFC822.HEADER']
                msg = email.message_from_bytes(header_data)

                result.append({
                    "id": str(msg_id),
                    "subject": _decode_mime_header(msg.get('Subject', '')),
                    "from": _decode_mime_header(msg.get('From', '')),
                    "to": _decode_mime_header(msg.get('To', '')),
                    "date": msg.get('Date', ''),
                    "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data[b'FLAGS']],
                    "folder": folder
                })
            except:
                continue

        return result


async def send_message(
    context: Context,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    html: bool = False
) -> Dict[str, str]:
    """
    Send an email message via SMTP.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body content
        cc: CC recipients (optional, comma-separated)
        bcc: BCC recipients (optional, comma-separated)
        html: Whether body is HTML (default: False)

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    # Create message
    msg = MIMEMultipart('alternative') if html else MIMEText(body)

    msg['From'] = username
    msg['To'] = to
    msg['Subject'] = subject

    if cc:
        msg['Cc'] = cc
    if bcc:
        msg['Bcc'] = bcc

    if html:
        msg.attach(MIMEText(body, 'html'))

    # Send via SMTP
    with _get_smtp_client(username, password) as client:
        recipients = [to]
        if cc:
            recipients.extend([addr.strip() for addr in cc.split(',')])
        if bcc:
            recipients.extend([addr.strip() for addr in bcc.split(',')])

        client.send_message(msg, from_addr=username, to_addrs=recipients)

    return {
        "status": "success",
        "message": f"Email sent to {to}"
    }


async def move_message(
    context: Context,
    message_id: str,
    from_folder: str,
    to_folder: str
) -> Dict[str, str]:
    """
    Move a message to another folder.

    Args:
        message_id: Message ID
        from_folder: Source folder
        to_folder: Destination folder

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    with _get_imap_client(username, password) as client:
        client.select_folder(from_folder)
        msg_id = int(message_id)

        # Copy to destination
        client.copy([msg_id], to_folder)

        # Delete from source
        client.delete_messages([msg_id])
        client.expunge()

    return {
        "status": "success",
        "message": f"Message {message_id} moved from {from_folder} to {to_folder}"
    }


async def delete_message(
    context: Context,
    message_id: str,
    folder: str = "INBOX",
    permanent: bool = False
) -> Dict[str, str]:
    """
    Delete a message.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)
        permanent: Permanently delete (True) or move to trash (False)

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    with _get_imap_client(username, password) as client:
        client.select_folder(folder)
        msg_id = int(message_id)

        if permanent:
            # Permanent deletion
            client.delete_messages([msg_id])
            client.expunge()
            message = f"Message {message_id} permanently deleted"
        else:
            # Move to Trash
            try:
                client.copy([msg_id], 'Trash')
                client.delete_messages([msg_id])
                client.expunge()
                message = f"Message {message_id} moved to Trash"
            except:
                # Fallback to permanent delete if Trash doesn't exist
                client.delete_messages([msg_id])
                client.expunge()
                message = f"Message {message_id} deleted"

    return {
        "status": "success",
        "message": message
    }


async def mark_as_read(
    context: Context,
    message_id: str,
    folder: str = "INBOX"
) -> Dict[str, str]:
    """
    Mark a message as read.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    with _get_imap_client(username, password) as client:
        client.select_folder(folder)
        msg_id = int(message_id)
        client.add_flags([msg_id], ['\\Seen'])

    return {
        "status": "success",
        "message": f"Message {message_id} marked as read"
    }


async def mark_as_unread(
    context: Context,
    message_id: str,
    folder: str = "INBOX"
) -> Dict[str, str]:
    """
    Mark a message as unread.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)

    Returns:
        Confirmation message
    """
    username, password = require_auth(context)

    with _get_imap_client(username, password) as client:
        client.select_folder(folder)
        msg_id = int(message_id)
        client.remove_flags([msg_id], ['\\Seen'])

    return {
        "status": "success",
        "message": f"Message {message_id} marked as unread"
    }
