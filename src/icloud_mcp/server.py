"""FastMCP server for iCloud integration."""

import logging
import sys

from fastmcp import Context, FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mcp.types import ToolAnnotations

from . import calendar, contacts
from . import email as email_module
from .auth import AuthenticationError
from .config import config

# Log unexpected tool failures to stderr; never surface exception text to the
# client (see the generic 500 branch in every tool below).
logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.ERROR)
_stderr_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(_stderr_handler)

# Optional bearer-token gate for the HTTP transport. MCP_AUTH_TOKEN unset ->
# auth=None -> gate disabled (back-compat with stdio and unauthenticated HTTP).
_auth = (
    StaticTokenVerifier(tokens={config.MCP_AUTH_TOKEN: {"client_id": "icloud-mcp", "scopes": []}})
    if config.MCP_AUTH_TOKEN
    else None
)

# mask_error_details keeps raw exception text (from explicitly-raised ToolError
# aside) out of client responses; the tool bodies below also avoid returning
# str(e) on the 500 path.
mcp = FastMCP("iCloud MCP Server", auth=_auth, mask_error_details=True)


# ============================================================================
# Health Check Endpoint
# ============================================================================

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint for Cloud Run and Docker."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "status": "healthy",
        "service": "icloud-mcp",
        "transport": "http"
    })


# ============================================================================
# Calendar Tools (CalDAV)
# ============================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def calendar_list_calendars(context: Context) -> list | dict:
    """
    List all available calendars.

    Returns a list of calendars with their IDs, names, and URLs.
    """
    try:
        return await calendar.list_calendars(context)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


MAX_LIST_RANGE_DAYS = 92


def validate_date_range(start_date: str, end_date: str) -> str | None:
    """Return an error message for a bad YYYY-MM-DD range, None when it is fine."""
    from datetime import date

    try:
        start = date.fromisoformat(str(start_date))
        end = date.fromisoformat(str(end_date))
    except (TypeError, ValueError):
        return "start_date and end_date are required in ISO format YYYY-MM-DD"
    if end < start:
        return "end_date must not be before start_date"
    if (end - start).days > MAX_LIST_RANGE_DAYS:
        return f"date range too long: at most {MAX_LIST_RANGE_DAYS} days between start_date and end_date"
    return None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def calendar_list_events(
    context: Context,
    start_date: str,
    end_date: str,
    calendar_id: str | None = None
) -> list | dict:
    """
    List calendar events in a date range. Both dates are REQUIRED: without a range the
    whole calendar would be returned. Keep the range short (one day to a few weeks, max 92 days).

    Args:
        start_date: Range start, ISO date YYYY-MM-DD (required, inclusive)
        end_date: Range end, ISO date YYYY-MM-DD (required, inclusive, at most 92 days after start_date)
        calendar_id: Specific calendar URL/ID (optional; default: all calendars)
    """
    range_error = validate_date_range(start_date, end_date)
    if range_error:
        return {"error": range_error, "status": 400}
    try:
        return await calendar.list_events(context, calendar_id, start_date, end_date)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def calendar_create_event(
    context: Context,
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    calendar_id: str | None = None
) -> dict:
    """
    Create a new calendar event.

    Args:
        summary: Event title
        start: Start datetime in ISO format (e.g., "2025-11-15T10:00:00")
        end: End datetime in ISO format (e.g., "2025-11-15T11:00:00")
        description: Event description (optional)
        location: Event location (optional)
        attendees: List of attendee email addresses to invite (optional)
        calendar_id: Target calendar URL/ID (optional)
    """
    try:
        return await calendar.create_event(context, summary, start, end, description, location, attendees, calendar_id)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def calendar_update_event(
    context: Context,
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None
) -> dict:
    """
    Update an existing calendar event.

    Args:
        event_id: Event URL/ID
        summary: New event title (optional)
        start: New start datetime in ISO format (optional)
        end: New end datetime in ISO format (optional)
        description: New description (optional)
        location: New location (optional)
        attendees: New list of attendee email addresses (optional, replaces existing)
    """
    try:
        return await calendar.update_event(context, event_id, summary, start, end, description, location, attendees)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def calendar_delete_event(context: Context, event_id: str) -> dict:
    """
    Delete a calendar event.

    Args:
        event_id: Event URL/ID to delete
    """
    try:
        return await calendar.delete_event(context, event_id)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def calendar_search_events(
    context: Context,
    query: str,
    calendar_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None
) -> list | dict:
    """
    Search for events by text query.

    Args:
        query: Search text (matches summary, description, location)
        calendar_id: Specific calendar URL/ID (optional)
        start_date: Start date in ISO format (optional)
        end_date: End date in ISO format (optional)
    """
    try:
        return await calendar.search_events(context, query, calendar_id, start_date, end_date)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


# ============================================================================
# Contacts Tools (CardDAV)
# ============================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def contacts_list(context: Context, limit: int | None = None) -> list | dict:
    """
    List all contacts.

    Args:
        limit: Maximum number of contacts to return (optional)
    """
    try:
        return await contacts.list_contacts(context, limit)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def contacts_get(context: Context, contact_id: str) -> dict:
    """
    Get a specific contact by ID.

    Args:
        contact_id: Contact URL/ID
    """
    try:
        return await contacts.get_contact(context, contact_id)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def contacts_create(
    context: Context,
    name: str,
    phones: list[str] | None = None,
    emails: list[str] | None = None,
    addresses: list[str] | None = None,
    organization: str | None = None,
    title: str | None = None
) -> dict:
    """
    Create a new contact.

    Args:
        name: Full name
        phones: List of phone numbers (optional)
        emails: List of email addresses (optional)
        addresses: List of postal addresses (optional)
        organization: Company/organization name (optional)
        title: Job title (optional)
    """
    try:
        return await contacts.create_contact(context, name, phones, emails, addresses, organization, title)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def contacts_update(
    context: Context,
    contact_id: str,
    name: str | None = None,
    phones: list[str] | None = None,
    emails: list[str] | None = None,
    addresses: list[str] | None = None,
    organization: str | None = None,
    title: str | None = None
) -> dict:
    """
    Update an existing contact.

    Args:
        contact_id: Contact URL/ID
        name: New full name (optional)
        phones: New list of phone numbers (optional)
        emails: New list of email addresses (optional)
        addresses: New list of postal addresses (optional)
        organization: New company/organization (optional)
        title: New job title (optional)
    """
    try:
        return await contacts.update_contact(context, contact_id, name, phones, emails, addresses, organization, title)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def contacts_delete(context: Context, contact_id: str) -> dict:
    """
    Delete a contact.

    Args:
        contact_id: Contact URL/ID to delete
    """
    try:
        return await contacts.delete_contact(context, contact_id)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def contacts_search(context: Context, query: str) -> list | dict:
    """
    Search for contacts by text query.

    Args:
        query: Search text (matches name, email, phone)
    """
    try:
        return await contacts.search_contacts(context, query)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


# ============================================================================
# Email Tools (IMAP/SMTP)
# ============================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def email_list_folders(context: Context) -> list | dict:
    """
    List all email folders/mailboxes.

    Returns a list of folders with their names and flags.
    """
    try:
        return await email_module.list_folders(context)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def email_list_messages(
    context: Context,
    folder: str = "INBOX",
    limit: int = 20,
    unread_only: bool = False
) -> list | dict:
    """
    List messages in a folder.

    Args:
        folder: Folder name (default: INBOX). Common folder names: INBOX, Sent Messages, Drafts, Trash, Archive
        limit: Maximum number of messages to return (default: 50)
        unread_only: Only return unread messages (default: False)

    Note: The Sent folder may be named "Sent Messages", "Sent", or "Sent Items" depending on your email provider.
    """
    try:
        return await email_module.list_messages(context, folder, limit, unread_only)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def email_get_message(
    context: Context,
    message_id: str,
    folder: str = "INBOX",
    include_body: bool = True,
    full_html: bool = False
) -> dict:
    """
    Get a specific message with full details.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)
        include_body: Include message body content (default: True)
        full_html: Include full HTML body (default: False, only text body returned)
    """
    try:
        return await email_module.get_message(context, message_id, folder, include_body, full_html)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def email_get_messages(
    context: Context,
    message_ids: list[str],
    folder: str = "INBOX",
    include_body: bool = True,
    full_html: bool = False
) -> list | dict:
    """
    Get multiple messages at once (bulk fetch).

    Args:
        message_ids: List of message IDs to fetch
        folder: Folder name (default: INBOX)
        include_body: Include message body content (default: True)
        full_html: Include full HTML body (default: False, only text body returned)
    """
    try:
        return await email_module.get_messages(context, message_ids, folder, include_body, full_html)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True))
async def email_search(
    context: Context,
    query: str,
    folder: str = "INBOX",
    limit: int = 20
) -> list | dict:
    """
    Search for messages by text query.

    Args:
        query: Search text (searches subject and from fields)
        folder: Folder name (default: INBOX)
        limit: Maximum number of results (default: 50)
    """
    try:
        return await email_module.search_messages(context, query, folder, limit)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def email_send(
    context: Context,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html: bool = False
) -> dict:
    """
    Send an email message via SMTP.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body content
        cc: CC recipients (optional, comma-separated)
        bcc: BCC recipients (optional, comma-separated)
        html: Whether body is HTML (default: False)
    """
    try:
        return await email_module.send_message(context, to, subject, body, cc, bcc, html)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def email_move(
    context: Context,
    message_id: str,
    from_folder: str,
    to_folder: str
) -> dict:
    """
    Move a message to another folder.

    Args:
        message_id: Message ID
        from_folder: Source folder
        to_folder: Destination folder
    """
    try:
        return await email_module.move_message(context, message_id, from_folder, to_folder)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def email_delete(
    context: Context,
    message_id: str,
    folder: str = "INBOX",
    permanent: bool = False
) -> dict:
    """
    Delete a message.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)
        permanent: Permanently delete (True) or move to trash (False)
    """
    try:
        return await email_module.delete_message(context, message_id, folder, permanent)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def email_mark_read(
    context: Context,
    message_id: str,
    folder: str = "INBOX"
) -> dict:
    """
    Mark a message as read.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)
    """
    try:
        return await email_module.mark_as_read(context, message_id, folder)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def email_mark_unread(
    context: Context,
    message_id: str,
    folder: str = "INBOX"
) -> dict:
    """
    Mark a message as unread.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)
    """
    try:
        return await email_module.mark_as_unread(context, message_id, folder)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception:
        logger.exception("Tool execution failed")
        return {"error": "Internal error", "status": 500}


# ============================================================================
# Server Entrypoint
# ============================================================================

def run():
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run()
