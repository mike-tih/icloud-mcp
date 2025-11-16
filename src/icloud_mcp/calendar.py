"""CalDAV tools for calendar management."""

import caldav
import smtplib
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from fastmcp import Context
from .auth import require_auth
from .config import config


def _get_caldav_client(email: str, password: str) -> caldav.DAVClient:
    """Create CalDAV client (stateless)."""
    return caldav.DAVClient(
        url=config.CALDAV_SERVER,
        username=email,
        password=password
    )


def _send_calendar_invitation(
    organizer_email: str,
    organizer_password: str,
    attendee_email: str,
    ical_data: str,
    summary: str,
    start: str,
    end: str,
    location: Optional[str] = None,
    method: str = "REQUEST"
) -> None:
    """
    Send calendar invitation via email (iTIP protocol).

    Args:
        organizer_email: Organizer's email address
        organizer_password: Organizer's password
        attendee_email: Attendee's email address
        ical_data: iCalendar data (VCALENDAR format)
        summary: Event summary
        start: Start datetime string
        end: End datetime string
        location: Event location (optional)
        method: iTIP method (REQUEST, CANCEL, etc.)
    """
    # Create multipart message
    msg = MIMEMultipart('alternative')
    msg['From'] = organizer_email
    msg['To'] = attendee_email
    msg['Subject'] = f"Invitation: {summary}"

    # Add Date header
    from email.utils import formatdate
    msg['Date'] = formatdate(localtime=True)

    # Create plain text part
    text_body = f"""You have been invited to the following event:

Summary: {summary}
Start: {start}
End: {end}"""

    if location:
        text_body += f"\nLocation: {location}"

    text_body += f"\n\nOrganizer: {organizer_email}"

    msg.attach(MIMEText(text_body, 'plain'))

    # Modify iCalendar data to include METHOD
    # Replace the first line with VCALENDAR and METHOD
    ical_lines = ical_data.strip().split('\n')
    if ical_lines[0] == 'BEGIN:VCALENDAR':
        # Insert METHOD after BEGIN:VCALENDAR
        ical_lines.insert(1, f'METHOD:{method}')
        ical_with_method = '\n'.join(ical_lines)
    else:
        ical_with_method = ical_data

    # Add organizer to the VEVENT if not present
    if 'ORGANIZER' not in ical_with_method:
        # Insert ORGANIZER after UID
        ical_lines = ical_with_method.split('\n')
        for i, line in enumerate(ical_lines):
            if line.startswith('UID:'):
                ical_lines.insert(i + 1, f'ORGANIZER;CN={organizer_email}:mailto:{organizer_email}')
                break
        ical_with_method = '\n'.join(ical_lines)

    # Create calendar part with proper content type
    cal_part = MIMEText(ical_with_method, 'calendar', 'utf-8')
    cal_part.add_header('Content-Class', 'urn:content-classes:calendarmessage')
    cal_part.add_header('Content-Type', f'text/calendar; method={method}; charset=UTF-8')
    msg.attach(cal_part)

    # Send via SMTP
    smtp_client = smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT)
    try:
        smtp_client.starttls()
        smtp_client.login(organizer_email, organizer_password)
        smtp_client.send_message(msg, from_addr=organizer_email, to_addrs=[attendee_email])
    finally:
        smtp_client.quit()

    # Save copy to Sent folder via IMAP (same as regular emails)
    try:
        from .email import _get_imap_client, _close_imap_client

        imap_client = _get_imap_client(organizer_email, organizer_password)
        try:
            # Convert message to bytes
            msg_bytes = msg.as_bytes()

            # Try to append to Sent folder
            try:
                imap_client.append(config.SENT_FOLDER, msg_bytes, flags=['\\Seen'])
            except Exception:
                # Try common alternatives
                for folder_name in ['Sent', 'Sent Items', config.SENT_FOLDER]:
                    try:
                        imap_client.append(folder_name, msg_bytes, flags=['\\Seen'])
                        break
                    except Exception:
                        continue
        finally:
            _close_imap_client(imap_client)
    except Exception:
        # Silently ignore errors saving to Sent folder
        pass


async def list_calendars(context: Context) -> List[Dict[str, Any]]:
    """
    List all available calendars.

    Returns:
        List of calendars with id, name, and description
    """
    email, password = require_auth(context)
    client = _get_caldav_client(email, password)
    principal = client.principal()
    calendars = principal.calendars()

    result = []
    for cal in calendars:
        result.append({
            "id": str(cal.url),
            "name": cal.name or "Unnamed Calendar",
            "url": str(cal.url)
        })

    return result


async def list_events(
    context: Context,
    calendar_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    List calendar events with optional filtering.

    Args:
        calendar_id: Specific calendar URL/ID (optional, defaults to all non-reminder calendars)
        start_date: Start date filter in ISO format (YYYY-MM-DD)
        end_date: End date filter in ISO format (YYYY-MM-DD)

    Returns:
        List of events with details
    """
    email, password = require_auth(context)
    client = _get_caldav_client(email, password)
    principal = client.principal()

    # Parse dates
    if start_date:
        start = datetime.fromisoformat(start_date)
        # If only date provided (no time), set to start of day
        if len(start_date) == 10:  # Format: YYYY-MM-DD
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = datetime.now() - timedelta(days=90)

    if end_date:
        end = datetime.fromisoformat(end_date)
        # If only date provided (no time), set to end of day
        if len(end_date) == 10:  # Format: YYYY-MM-DD
            # Add one day to include the entire end date
            end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        end = datetime.now() + timedelta(days=365)

    result = []

    # Get calendars
    if calendar_id:
        calendars_to_search = [caldav.Calendar(client=client, url=calendar_id)]
    else:
        all_calendars = principal.calendars()
        if not all_calendars:
            return []

        # Filter out reminder calendars (they don't have events in the same format)
        calendars_to_search = [
            cal for cal in all_calendars
            if cal.name and '⚠' not in cal.name and 'reminder' not in cal.name.lower()
        ]

        # If all calendars are filtered out, search all
        if not calendars_to_search:
            calendars_to_search = all_calendars

    # Search events in all relevant calendars
    for calendar in calendars_to_search:
        try:
            # Fetch events using date_search
            events = calendar.date_search(start=start, end=end, expand=True)

            for event in events:
                try:
                    event.load()  # Ensure event data is loaded
                    vevent = event.vobject_instance.vevent

                    # Parse start/end dates safely
                    start_value = None
                    end_value = None

                    if hasattr(vevent, 'dtstart') and vevent.dtstart:
                        try:
                            start_value = vevent.dtstart.value
                            if hasattr(start_value, 'isoformat'):
                                start_value = start_value.isoformat()
                            else:
                                start_value = str(start_value)
                        except Exception as _e:
                            pass

                    if hasattr(vevent, 'dtend') and vevent.dtend:
                        try:
                            end_value = vevent.dtend.value
                            if hasattr(end_value, 'isoformat'):
                                end_value = end_value.isoformat()
                            else:
                                end_value = str(end_value)
                        except Exception as _e:
                            pass

                    result.append({
                        "id": str(event.url),
                        "summary": str(vevent.summary.value) if hasattr(vevent, 'summary') and vevent.summary else "",
                        "description": str(vevent.description.value) if hasattr(vevent, 'description') and vevent.description else "",
                        "start": start_value,
                        "end": end_value,
                        "location": str(vevent.location.value) if hasattr(vevent, 'location') and vevent.location else "",
                        "calendar": calendar.name or "Unknown",
                        "url": str(event.url)
                    })
                except Exception as _e:
                    # Skip malformed events
                    continue
        except Exception as _e:
            # Skip calendars that fail to search
            continue

    return result


async def create_event(
    context: Context,
    summary: str,
    start: str,
    end: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    calendar_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new calendar event.

    Args:
        summary: Event title
        start: Start datetime in ISO format
        end: End datetime in ISO format
        description: Event description (optional)
        location: Event location (optional)
        attendees: List of attendee email addresses to invite (optional)
        calendar_id: Target calendar URL/ID (optional, defaults to first non-reminder calendar)

    Returns:
        Created event details
    """
    email, password = require_auth(context)
    client = _get_caldav_client(email, password)
    principal = client.principal()

    # Get calendar
    if calendar_id:
        calendar = caldav.Calendar(client=client, url=calendar_id)
    else:
        all_calendars = principal.calendars()
        if not all_calendars:
            raise ValueError("No calendars found")

        # Filter out reminder/task calendars - they don't support VEVENT
        event_calendars = [
            cal for cal in all_calendars
            if cal.name and '⚠' not in cal.name and 'reminder' not in cal.name.lower()
        ]

        if not event_calendars:
            raise ValueError("No event calendars found (only reminder/task calendars available)")

        calendar = event_calendars[0]

    # Build iCalendar data with proper formatting for iCloud
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    now = datetime.now()

    # Generate UID without dots (iCloud compatible)
    uid = f"{int(now.timestamp())}{now.microsecond}@icloud-mcp"

    # Build proper iCalendar format (iCloud is very strict about formatting)
    ical_data = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//iCloud MCP//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}
DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}
DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}
SUMMARY:{summary}
STATUS:CONFIRMED
SEQUENCE:0
"""

    if description:
        # Escape special characters in description
        desc_escaped = description.replace('\\', '\\\\').replace(',', '\\,').replace(';', '\\;').replace('\n', '\\n')
        ical_data += f"DESCRIPTION:{desc_escaped}\n"
    if location:
        loc_escaped = location.replace('\\', '\\\\').replace(',', '\\,').replace(';', '\\;')
        ical_data += f"LOCATION:{loc_escaped}\n"

    # Add attendees (meeting invitations)
    if attendees:
        for attendee_email in attendees:
            # Format: ATTENDEE;CN=email;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:email
            ical_data += f"ATTENDEE;CN={attendee_email};CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{attendee_email}\n"

    ical_data += "END:VEVENT\nEND:VCALENDAR"

    # Create event using add_event (more reliable than save_event for iCloud)
    try:
        event = calendar.add_event(ical_data)
    except Exception as e:
        # If add_event fails, try save_event as fallback
        raise ValueError(f"Failed to create event in calendar '{calendar.name}': {str(e)}")

    # Send email invitations to attendees (iTIP protocol)
    if attendees:
        for attendee_email in attendees:
            try:
                _send_calendar_invitation(
                    organizer_email=email,
                    organizer_password=password,
                    attendee_email=attendee_email,
                    ical_data=ical_data,
                    summary=summary,
                    start=start,
                    end=end,
                    location=location,
                    method="REQUEST"
                )
            except Exception as e:
                # Log error but don't fail the event creation
                # The event is already created, we just failed to send the invitation
                import logging
                logging.error(f"Failed to send invitation to {attendee_email}: {e}")

    return {
        "id": str(event.url),
        "summary": summary,
        "start": start,
        "end": end,
        "description": description or "",
        "location": location or "",
        "attendees": attendees or [],
        "calendar": calendar.name,
        "url": str(event.url)
    }


async def update_event(
    context: Context,
    event_id: str,
    summary: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None
) -> Dict[str, Any]:
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

    Returns:
        Updated event details
    """
    email, password = require_auth(context)

    # Create a client with the correct base URL for this specific event
    # This prevents URL joining errors when event is on a different server (e.g., p72-caldav.icloud.com)
    parsed = urlparse(event_id)
    event_base_url = f"{parsed.scheme}://{parsed.netloc}"
    event_client = caldav.DAVClient(url=event_base_url, username=email, password=password)

    try:
        # Load existing event using CalendarObjectResource
        event = caldav.CalendarObjectResource(client=event_client, url=event_id)
        event.load()
    except Exception as e:
        raise Exception(f"Error loading event: {str(e)}")

    vevent = event.vobject_instance.vevent

    # Update fields
    if summary:
        vevent.summary.value = summary
    if start:
        vevent.dtstart.value = datetime.fromisoformat(start)
    if end:
        vevent.dtend.value = datetime.fromisoformat(end)
    if description is not None:
        if hasattr(vevent, 'description'):
            vevent.description.value = description
        else:
            vevent.add('description').value = description
    if location is not None:
        if hasattr(vevent, 'location'):
            vevent.location.value = location
        else:
            vevent.add('location').value = location

    # Update attendees
    if attendees is not None:
        # Remove existing attendees
        if hasattr(vevent, 'attendee_list'):
            for att in list(vevent.attendee_list):
                vevent.remove(att)

        # Add new attendees
        for attendee_email in attendees:
            att = vevent.add('attendee')
            att.value = f'mailto:{attendee_email}'
            att.params['CN'] = [attendee_email]
            att.params['CUTYPE'] = ['INDIVIDUAL']
            att.params['ROLE'] = ['REQ-PARTICIPANT']
            att.params['PARTSTAT'] = ['NEEDS-ACTION']
            att.params['RSVP'] = ['TRUE']

    # Save changes - use PUT request directly to avoid parent dependency
    try:
        # Serialize the updated vCalendar data and send PUT request
        updated_ical = event.vobject_instance.serialize()
        event_client.put(event_id, updated_ical, {"Content-Type": "text/calendar; charset=utf-8"})
    except Exception as e:
        raise Exception(f"Error saving event: {str(e)}")

    # Extract attendees for response
    attendee_list = []
    if hasattr(vevent, 'attendee_list'):
        for att in vevent.attendee_list:
            if hasattr(att, 'value'):
                email_addr = str(att.value).replace('mailto:', '')
                attendee_list.append(email_addr)

    # Send update notifications to attendees if attendees were modified
    if attendees is not None and attendee_list:
        event_summary = str(vevent.summary.value) if hasattr(vevent, 'summary') else ""
        event_start = vevent.dtstart.value.isoformat() if hasattr(vevent, 'dtstart') else start
        event_end = vevent.dtend.value.isoformat() if hasattr(vevent, 'dtend') else end
        event_location = str(vevent.location.value) if hasattr(vevent, 'location') else None

        for attendee_email in attendee_list:
            try:
                _send_calendar_invitation(
                    organizer_email=email,
                    organizer_password=password,
                    attendee_email=attendee_email,
                    ical_data=updated_ical,
                    summary=event_summary,
                    start=event_start,
                    end=event_end,
                    location=event_location,
                    method="REQUEST"  # Use REQUEST for updates too
                )
            except Exception as e:
                # Log error but don't fail the update
                import logging
                logging.error(f"Failed to send update notification to {attendee_email}: {e}")

    return {
        "id": str(event.url),
        "summary": str(vevent.summary.value) if hasattr(vevent, 'summary') else "",
        "start": vevent.dtstart.value.isoformat() if hasattr(vevent, 'dtstart') else None,
        "end": vevent.dtend.value.isoformat() if hasattr(vevent, 'dtend') else None,
        "description": str(vevent.description.value) if hasattr(vevent, 'description') else "",
        "location": str(vevent.location.value) if hasattr(vevent, 'location') else "",
        "attendees": attendee_list,
        "url": str(event.url)
    }


async def delete_event(context: Context, event_id: str) -> Dict[str, str]:
    """
    Delete a calendar event.

    Args:
        event_id: Event URL/ID to delete

    Returns:
        Confirmation message
    """
    email, password = require_auth(context)

    # Create a client with the correct base URL for this specific event
    # This prevents URL joining errors when event is on a different server (e.g., p72-caldav.icloud.com)
    parsed = urlparse(event_id)
    event_base_url = f"{parsed.scheme}://{parsed.netloc}"
    event_client = caldav.DAVClient(url=event_base_url, username=email, password=password)

    # Use CalendarObjectResource to handle full URLs correctly
    event = caldav.CalendarObjectResource(client=event_client, url=event_id)

    # Load event to get attendees before deleting
    attendee_list = []
    event_summary = ""
    event_start = ""
    event_end = ""
    event_location = None
    ical_data = None

    try:
        event.load()
        vevent = event.vobject_instance.vevent

        # Extract event details
        event_summary = str(vevent.summary.value) if hasattr(vevent, 'summary') else "Event"
        if hasattr(vevent, 'dtstart'):
            event_start = vevent.dtstart.value.isoformat() if hasattr(vevent.dtstart.value, 'isoformat') else str(vevent.dtstart.value)
        if hasattr(vevent, 'dtend'):
            event_end = vevent.dtend.value.isoformat() if hasattr(vevent.dtend.value, 'isoformat') else str(vevent.dtend.value)
        if hasattr(vevent, 'location'):
            event_location = str(vevent.location.value)

        # Extract attendees
        if hasattr(vevent, 'attendee_list'):
            for att in vevent.attendee_list:
                if hasattr(att, 'value'):
                    email_addr = str(att.value).replace('mailto:', '')
                    attendee_list.append(email_addr)

        # Get the iCalendar data for CANCEL notifications
        ical_data = event.vobject_instance.serialize()

    except Exception as e:
        # If we can't load the event, just delete it
        import logging
        logging.warning(f"Could not load event details before deletion: {e}")

    # Delete the event
    event.delete()

    # Send cancellation notifications to attendees
    if attendee_list and ical_data:
        for attendee_email in attendee_list:
            try:
                _send_calendar_invitation(
                    organizer_email=email,
                    organizer_password=password,
                    attendee_email=attendee_email,
                    ical_data=ical_data,
                    summary=event_summary,
                    start=event_start,
                    end=event_end,
                    location=event_location,
                    method="CANCEL"
                )
            except Exception as e:
                # Log error but don't fail the deletion
                import logging
                logging.error(f"Failed to send cancellation to {attendee_email}: {e}")

    return {"status": "success", "message": f"Event {event_id} deleted"}


async def search_events(
    context: Context,
    query: str,
    calendar_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Search for events by text query.

    Args:
        query: Search text (matches summary and description)
        calendar_id: Specific calendar URL/ID (optional)
        start_date: Start date filter in ISO format (optional)
        end_date: End date filter in ISO format (optional)

    Returns:
        List of matching events
    """
    # Get all events
    events = await list_events(context, calendar_id, start_date, end_date)

    # Filter by query
    query_lower = query.lower()
    filtered_events = [
        event for event in events
        if query_lower in event.get("summary", "").lower()
        or query_lower in event.get("description", "").lower()
        or query_lower in event.get("location", "").lower()
    ]

    return filtered_events
