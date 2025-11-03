"""CardDAV tools for contacts management."""

import caldav
import vobject
from typing import List, Dict, Any, Optional
from fastmcp import Context
from .auth import require_auth
from .config import config


def _get_carddav_client(email: str, password: str) -> caldav.DAVClient:
    """Create CardDAV client (stateless)."""
    return caldav.DAVClient(
        url=config.CARDDAV_SERVER,
        username=email,
        password=password
    )


async def list_contacts(
    context: Context,
    limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    List all contacts.

    Args:
        limit: Maximum number of contacts to return (optional)

    Returns:
        List of contacts with name, phone, email, address
    """
    email, password = require_auth(context)
    client = _get_carddav_client(email, password)

    try:
        principal = client.principal()
    except Exception as e:
        raise ValueError(f"Failed to connect to CardDAV server: {str(e)}")

    # Get address books (try multiple methods for compatibility)
    address_books = []
    try:
        # Try standard method first
        address_books = principal.calendars()
    except:
        try:
            # Alternative: try to get all collections
            address_books = list(principal.addressbooks())
        except:
            pass

    if not address_books:
        return []

    # Get contacts from first address book
    address_book = address_books[0]

    # Fetch all vCards
    vcards = []
    try:
        vcards = list(address_book.objects())
    except Exception as e:
        # If objects() fails, try alternative methods
        try:
            vcards = list(address_book.search())
        except:
            pass

    result = []
    count = 0

    for vcard_obj in vcards:
        if limit and count >= limit:
            break

        try:
            # Safely get vCard data
            vcard_data = vcard_obj.data
            if not vcard_data or len(vcard_data) == 0:
                continue

            vcard = vobject.readOne(vcard_data)

            contact = {
                "id": str(vcard_obj.url) if hasattr(vcard_obj, 'url') else "",
                "name": "",
                "phones": [],
                "emails": [],
                "addresses": [],
                "url": str(vcard_obj.url) if hasattr(vcard_obj, 'url') else ""
            }

            # Safely extract name
            if hasattr(vcard, 'fn') and vcard.fn and hasattr(vcard.fn, 'value'):
                contact["name"] = str(vcard.fn.value)

            # Extract phone numbers
            if hasattr(vcard, 'tel_list'):
                for tel in vcard.tel_list:
                    if hasattr(tel, 'value') and tel.value:
                        contact["phones"].append(str(tel.value))

            # Extract emails
            if hasattr(vcard, 'email_list'):
                for em in vcard.email_list:
                    if hasattr(em, 'value') and em.value:
                        contact["emails"].append(str(em.value))

            # Extract addresses (safe conversion)
            if hasattr(vcard, 'adr_list'):
                for adr in vcard.adr_list:
                    if hasattr(adr, 'value'):
                        try:
                            # Try to convert to string safely
                            addr_str = str(adr.value) if adr.value else ""
                            if addr_str:
                                contact["addresses"].append(addr_str)
                        except:
                            continue

            # Only add contact if it has a name or at least one other field
            if contact["name"] or contact["phones"] or contact["emails"]:
                result.append(contact)
                count += 1

        except Exception as e:
            # Skip malformed vCards, log error if needed
            continue

    return result


async def get_contact(context: Context, contact_id: str) -> Dict[str, Any]:
    """
    Get a specific contact by ID.

    Args:
        contact_id: Contact URL/ID

    Returns:
        Contact details
    """
    email, password = require_auth(context)
    client = _get_carddav_client(email, password)

    # Load contact
    vcard_obj = caldav.CalendarObjectResource(client=client, url=contact_id)
    vcard_obj.load()

    vcard_data = vcard_obj.data
    vcard = vobject.readOne(vcard_data)

    contact = {
        "id": str(vcard_obj.url),
        "name": str(vcard.fn.value) if hasattr(vcard, 'fn') else "",
        "phones": [],
        "emails": [],
        "addresses": [],
        "organization": str(vcard.org.value[0]) if hasattr(vcard, 'org') else "",
        "title": str(vcard.title.value) if hasattr(vcard, 'title') else "",
        "url": str(vcard_obj.url)
    }

    # Extract phone numbers
    if hasattr(vcard, 'tel_list'):
        for tel in vcard.tel_list:
            contact["phones"].append(str(tel.value))

    # Extract emails
    if hasattr(vcard, 'email_list'):
        for em in vcard.email_list:
            contact["emails"].append(str(em.value))

    # Extract addresses
    if hasattr(vcard, 'adr_list'):
        for adr in vcard.adr_list:
            contact["addresses"].append(str(adr.value))

    return contact


async def create_contact(
    context: Context,
    name: str,
    phones: Optional[List[str]] = None,
    emails: Optional[List[str]] = None,
    addresses: Optional[List[str]] = None,
    organization: Optional[str] = None,
    title: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new contact.

    Args:
        name: Full name
        phones: List of phone numbers (optional)
        emails: List of email addresses (optional)
        addresses: List of postal addresses (optional)
        organization: Company/organization name (optional)
        title: Job title (optional)

    Returns:
        Created contact details
    """
    email, password = require_auth(context)
    client = _get_carddav_client(email, password)
    principal = client.principal()

    # Get address book
    address_books = principal.calendars()
    if not address_books:
        raise ValueError("No address books found")
    address_book = address_books[0]

    # Create vCard
    vcard = vobject.vCard()
    vcard.add('fn').value = name
    vcard.add('n').value = vobject.vcard.Name(family='', given=name)

    # Add phones
    if phones:
        for phone in phones:
            tel = vcard.add('tel')
            tel.value = phone
            tel.type_param = 'CELL'

    # Add emails
    if emails:
        for em in emails:
            email_obj = vcard.add('email')
            email_obj.value = em
            email_obj.type_param = 'INTERNET'

    # Add addresses
    if addresses:
        for addr in addresses:
            adr = vcard.add('adr')
            adr.value = vobject.vcard.Address(street=addr)

    # Add organization
    if organization:
        vcard.add('org').value = [organization]

    # Add title
    if title:
        vcard.add('title').value = title

    # Save contact
    vcard_data = vcard.serialize()
    contact_obj = address_book.save_event(vcard_data)

    return {
        "id": str(contact_obj.url),
        "name": name,
        "phones": phones or [],
        "emails": emails or [],
        "addresses": addresses or [],
        "organization": organization or "",
        "title": title or "",
        "url": str(contact_obj.url)
    }


async def update_contact(
    context: Context,
    contact_id: str,
    name: Optional[str] = None,
    phones: Optional[List[str]] = None,
    emails: Optional[List[str]] = None,
    addresses: Optional[List[str]] = None,
    organization: Optional[str] = None,
    title: Optional[str] = None
) -> Dict[str, Any]:
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

    Returns:
        Updated contact details
    """
    email, password = require_auth(context)
    client = _get_carddav_client(email, password)

    # Load existing contact
    vcard_obj = caldav.CalendarObjectResource(client=client, url=contact_id)
    vcard_obj.load()

    vcard = vobject.readOne(vcard_obj.data)

    # Update fields
    if name:
        vcard.fn.value = name

    if phones is not None:
        # Remove existing phones
        if hasattr(vcard, 'tel_list'):
            for tel in list(vcard.tel_list):
                vcard.remove(tel)
        # Add new phones
        for phone in phones:
            tel = vcard.add('tel')
            tel.value = phone
            tel.type_param = 'CELL'

    if emails is not None:
        # Remove existing emails
        if hasattr(vcard, 'email_list'):
            for em in list(vcard.email_list):
                vcard.remove(em)
        # Add new emails
        for em in emails:
            email_obj = vcard.add('email')
            email_obj.value = em
            email_obj.type_param = 'INTERNET'

    if addresses is not None:
        # Remove existing addresses
        if hasattr(vcard, 'adr_list'):
            for adr in list(vcard.adr_list):
                vcard.remove(adr)
        # Add new addresses
        for addr in addresses:
            adr = vcard.add('adr')
            adr.value = vobject.vcard.Address(street=addr)

    if organization is not None:
        if hasattr(vcard, 'org'):
            vcard.org.value = [organization]
        else:
            vcard.add('org').value = [organization]

    if title is not None:
        if hasattr(vcard, 'title'):
            vcard.title.value = title
        else:
            vcard.add('title').value = title

    # Save changes
    vcard_obj.data = vcard.serialize()
    vcard_obj.save()

    return {
        "id": str(vcard_obj.url),
        "name": str(vcard.fn.value) if hasattr(vcard, 'fn') else "",
        "phones": phones if phones is not None else [],
        "emails": emails if emails is not None else [],
        "addresses": addresses if addresses is not None else [],
        "organization": organization or "",
        "title": title or "",
        "url": str(vcard_obj.url)
    }


async def delete_contact(context: Context, contact_id: str) -> Dict[str, str]:
    """
    Delete a contact.

    Args:
        contact_id: Contact URL/ID to delete

    Returns:
        Confirmation message
    """
    email, password = require_auth(context)
    client = _get_carddav_client(email, password)

    vcard_obj = caldav.CalendarObjectResource(client=client, url=contact_id)
    vcard_obj.delete()

    return {"status": "success", "message": f"Contact {contact_id} deleted"}


async def search_contacts(
    context: Context,
    query: str
) -> List[Dict[str, Any]]:
    """
    Search for contacts by text query.

    Args:
        query: Search text (matches name, email, phone)

    Returns:
        List of matching contacts
    """
    # Get all contacts
    contacts = await list_contacts(context)

    # Filter by query
    query_lower = query.lower()
    filtered_contacts = [
        contact for contact in contacts
        if query_lower in contact.get("name", "").lower()
        or any(query_lower in email.lower() for email in contact.get("emails", []))
        or any(query_lower in phone.lower() for phone in contact.get("phones", []))
    ]

    return filtered_contacts
