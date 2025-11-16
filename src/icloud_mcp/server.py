"""FastMCP server for iCloud integration."""

from fastmcp import FastMCP
from . import calendar, contacts, email as email_module
from .auth import AuthenticationError

# Initialize FastMCP server
mcp = FastMCP("iCloud MCP Server")


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
        "transport": "sse"
    })


# ============================================================================
# Calendar Tools (CalDAV)
# ============================================================================

@mcp.tool()
async def calendar_list_calendars(context) -> list:
    """
    List all available calendars.

    Returns a list of calendars with their IDs, names, and URLs.
    """
    try:
        return await calendar.list_calendars(context)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def calendar_list_events(
    context,
    calendar_id: str = None,
    start_date: str = None,
    end_date: str = None
) -> list:
    """
    List calendar events with optional filtering.

    Args:
        calendar_id: Specific calendar URL/ID (optional)
        start_date: Start date in ISO format YYYY-MM-DD (optional)
        end_date: End date in ISO format YYYY-MM-DD (optional)
    """
    try:
        return await calendar.list_events(context, calendar_id, start_date, end_date)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def calendar_create_event(
    context,
    summary: str,
    start: str,
    end: str,
    description: str = None,
    location: str = None,
    attendees: list[str] = None,
    calendar_id: str = None
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def calendar_update_event(
    context,
    event_id: str,
    summary: str = None,
    start: str = None,
    end: str = None,
    description: str = None,
    location: str = None,
    attendees: list[str] = None
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def calendar_delete_event(context, event_id: str) -> dict:
    """
    Delete a calendar event.

    Args:
        event_id: Event URL/ID to delete
    """
    try:
        return await calendar.delete_event(context, event_id)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def calendar_search_events(
    context,
    query: str,
    calendar_id: str = None,
    start_date: str = None,
    end_date: str = None
) -> list:
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
    except Exception as e:
        return {"error": str(e), "status": 500}


# ============================================================================
# Contacts Tools (CardDAV)
# ============================================================================

@mcp.tool()
async def contacts_list(context, limit: int = None) -> list:
    """
    List all contacts.

    Args:
        limit: Maximum number of contacts to return (optional)
    """
    try:
        return await contacts.list_contacts(context, limit)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def contacts_get(context, contact_id: str) -> dict:
    """
    Get a specific contact by ID.

    Args:
        contact_id: Contact URL/ID
    """
    try:
        return await contacts.get_contact(context, contact_id)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def contacts_create(
    context,
    name: str,
    phones: list[str] = None,
    emails: list[str] = None,
    addresses: list[str] = None,
    organization: str = None,
    title: str = None
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def contacts_update(
    context,
    contact_id: str,
    name: str = None,
    phones: list[str] = None,
    emails: list[str] = None,
    addresses: list[str] = None,
    organization: str = None,
    title: str = None
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def contacts_delete(context, contact_id: str) -> dict:
    """
    Delete a contact.

    Args:
        contact_id: Contact URL/ID to delete
    """
    try:
        return await contacts.delete_contact(context, contact_id)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def contacts_search(context, query: str) -> list:
    """
    Search for contacts by text query.

    Args:
        query: Search text (matches name, email, phone)
    """
    try:
        return await contacts.search_contacts(context, query)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception as e:
        return {"error": str(e), "status": 500}


# ============================================================================
# Email Tools (IMAP/SMTP)
# ============================================================================

@mcp.tool()
async def email_list_folders(context) -> list:
    """
    List all email folders/mailboxes.

    Returns a list of folders with their names and flags.
    """
    try:
        return await email_module.list_folders(context)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def email_list_messages(
    context,
    folder: str = "INBOX",
    limit: int = 50,
    unread_only: bool = False
) -> list:
    """
    List messages in a folder.

    Args:
        folder: Folder name (default: INBOX)
        limit: Maximum number of messages to return (default: 50)
        unread_only: Only return unread messages (default: False)
    """
    try:
        return await email_module.list_messages(context, folder, limit, unread_only)
    except AuthenticationError as e:
        return {"error": str(e), "status": 401}
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def email_get_message(
    context,
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def email_get_messages(
    context,
    message_ids: list[str],
    folder: str = "INBOX",
    include_body: bool = True,
    full_html: bool = False
) -> list:
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def email_search(
    context,
    query: str,
    folder: str = "INBOX",
    limit: int = 50
) -> list:
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def email_send(
    context,
    to: str,
    subject: str,
    body: str,
    cc: str = None,
    bcc: str = None,
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def email_move(
    context,
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def email_delete(
    context,
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def email_mark_read(
    context,
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
    except Exception as e:
        return {"error": str(e), "status": 500}


@mcp.tool()
async def email_mark_unread(
    context,
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
    except Exception as e:
        return {"error": str(e), "status": 500}


# ============================================================================
# Server Entrypoint
# ============================================================================

def run():
    """Run the MCP server."""
    from .config import config as app_config
    mcp.run(transport="stdio")


def run_http():
    """Run the MCP server with HTTP transport."""
    from .config import config as app_config
    mcp.run(transport="sse", port=app_config.MCP_SERVER_PORT)


if __name__ == "__main__":
    run()
