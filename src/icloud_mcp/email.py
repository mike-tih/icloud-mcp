"""IMAP/SMTP tools for email management."""

import imaplib
import smtplib
import email
import logging
import sys
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from typing import List, Dict, Any, Optional
from datetime import datetime
from fastmcp import Context
from imapclient import IMAPClient
from .auth import require_auth
from .config import config

# Configure minimal logging (only errors)
logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)

# Log errors to stderr
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.ERROR)
stderr_handler.setFormatter(formatter)
logger.addHandler(stderr_handler)


def _get_imap_client(username: str, password: str) -> IMAPClient:
    """Create IMAP client (stateless)."""
    client = IMAPClient(config.IMAP_SERVER, port=config.IMAP_PORT, ssl=True, use_uid=True)
    client.login(username, password)
    return client


def _close_imap_client(client: IMAPClient) -> None:
    """Safely close IMAP client connection."""
    try:
        # Don't call logout() - it causes "file property has no setter" error in Python 3.14+
        # Just close the underlying socket
        if hasattr(client, '_imap') and hasattr(client._imap, 'sock'):
            client._imap.sock.close()
    except Exception as _e:
        pass  # Silently ignore errors on close


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
            except Exception as _e:
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
    try:
        username, password = require_auth(context)

        client = _get_imap_client(username, password)

        folders = client.list_folders()

        result = []
        for flags, delimiter, name in folders:
            result.append({
                "name": name,
                "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in flags],
                "delimiter": delimiter
            })

        return result
    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


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
    """
    try:
        username, password = require_auth(context)

        client = _get_imap_client(username, password)

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
            except Exception as _e:
                continue

        return result

    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

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
    try:
        username, password = require_auth(context)
        client = _get_imap_client(username, password)

        client.select_folder(folder)

        msg_id = int(message_id)

        # Use BODY.PEEK[] instead of RFC822 - more reliable with IMAPClient
        response = client.fetch([msg_id], [b'FLAGS', b'BODY.PEEK[]'])

        if msg_id not in response:
            raise ValueError(f"Message {message_id} not found")

        data = response[msg_id]

        # Try multiple possible keys for the message body
        raw_email = None
        for key in [b'BODY[]', 'BODY[]', b'RFC822', 'RFC822', b'BODY.PEEK[]']:
            if key in data:
                raw_email = data[key]
                break

        if raw_email is None:
            # Log available keys for debugging
            available_keys = list(data.keys())
            raise KeyError(f"Message body not found. Available keys: {available_keys}")

        msg = email.message_from_bytes(raw_email)

        result = {
            "id": message_id,
            "subject": _decode_mime_header(msg.get('Subject', '')),
            "from": _decode_mime_header(msg.get('From', '')),
            "to": _decode_mime_header(msg.get('To', '')),
            "cc": _decode_mime_header(msg.get('Cc', '')),
            "date": msg.get('Date', ''),
            "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data.get(b'FLAGS', data.get('FLAGS', []))],
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
                        except Exception as _e:
                            pass
                    elif content_type == "text/html":
                        try:
                            body_html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except Exception as _e:
                            pass
            else:
                try:
                    body_text = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                except Exception as _e:
                    pass

            result["body_text"] = body_text
            result["body_html"] = body_html

        return result

    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def get_messages(
    context: Context,
    message_ids: List[str],
    folder: str = "INBOX",
    include_body: bool = True
) -> List[Dict[str, Any]]:
    """
    Get multiple messages at once.

    Args:
        message_ids: List of message IDs to fetch
        folder: Folder name (default: INBOX)
        include_body: Include message body content

    Returns:
        List of message details
    """
    try:
        username, password = require_auth(context)
        client = _get_imap_client(username, password)

        client.select_folder(folder)

        # Convert string IDs to integers
        msg_ids = [int(mid) for mid in message_ids]

        # Fetch all messages at once
        response = client.fetch(msg_ids, [b'FLAGS', b'BODY.PEEK[]'])

        results = []

        for msg_id in msg_ids:
            if msg_id not in response:
                # Skip missing messages
                continue

            data = response[msg_id]

            # Try multiple possible keys for the message body
            raw_email = None
            for key in [b'BODY[]', 'BODY[]', b'RFC822', 'RFC822', b'BODY.PEEK[]']:
                if key in data:
                    raw_email = data[key]
                    break

            if raw_email is None:
                # Skip messages without body
                continue

            msg = email.message_from_bytes(raw_email)

            result = {
                "id": str(msg_id),
                "subject": _decode_mime_header(msg.get('Subject', '')),
                "from": _decode_mime_header(msg.get('From', '')),
                "to": _decode_mime_header(msg.get('To', '')),
                "cc": _decode_mime_header(msg.get('Cc', '')),
                "date": msg.get('Date', ''),
                "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in data.get(b'FLAGS', data.get('FLAGS', []))],
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
                            except Exception as _e:
                                pass
                        elif content_type == "text/html":
                            try:
                                body_html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            except Exception as _e:
                                pass
                else:
                    try:
                        body_text = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except Exception as _e:
                        pass

                result["body_text"] = body_text
                result["body_html"] = body_html

            results.append(result)

        return results

    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

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
    client = _get_imap_client(username, password)

    try:
        client.select_folder(folder)

        # Try server-side search with UTF-8 charset (RFC 2978)
        # This works with modern IMAP servers including iCloud
        try:
            # Search by subject or from using UTF-8 charset
            messages = client.search(
                ['OR', ['SUBJECT', query], ['FROM', query]],
                charset='UTF-8'
            )

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
                except Exception as _e:
                    continue

            return result

        except Exception as charset_error:
            # Fallback: If CHARSET UTF-8 is not supported by server,
            # fall back to local filtering (less efficient but always works)
            logger.error(f"Server-side UTF-8 search failed: {charset_error}. Falling back to local filtering.")

            # Fetch more messages to search through locally
            fetch_limit = max(limit * 10, 200)
            all_messages = await list_messages(context, folder, fetch_limit, unread_only=False)

            # Filter messages locally (supports any Unicode)
            query_lower = query.lower()
            filtered_messages = [
                msg for msg in all_messages
                if query_lower in msg.get("subject", "").lower()
                or query_lower in msg.get("from", "").lower()
                or query_lower in msg.get("to", "").lower()
            ]

            return filtered_messages[:limit]

    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

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

    client = _get_imap_client(username, password)
    
    try:
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
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

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

    client = _get_imap_client(username, password)
    
    try:
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
            except Exception as _e:
                # Fallback to permanent delete if Trash doesn't exist
                client.delete_messages([msg_id])
                client.expunge()
                message = f"Message {message_id} deleted"

        return {
            "status": "success",
            "message": message
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

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

    client = _get_imap_client(username, password)
    
    try:
        client.select_folder(folder)
        msg_id = int(message_id)
        client.add_flags([msg_id], ['\\Seen'])

        return {
            "status": "success",
            "message": f"Message {message_id} marked as read"
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass

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

    client = _get_imap_client(username, password)
    
    try:
        client.select_folder(folder)
        msg_id = int(message_id)
        client.remove_flags([msg_id], ['\\Seen'])

        return {
            "status": "success",
            "message": f"Message {message_id} marked as unread"
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass
