# iCloud MCP Server

MCP (Model Context Protocol) server for iCloud integration, providing tools for managing calendars (CalDAV), contacts (CardDAV), and email (IMAP/SMTP).

## Features

- **Stateless Architecture**: No state stored between requests
- **Full CRUD Operations**: Complete management of calendars, contacts, and email
- **Flexible Authentication**: Via headers or environment variables
- **Multiple Transports**: stdio (local) or HTTP/SSE (server)
- **Docker Support**: Easy deployment with Docker and Docker Compose

## Supported Operations

### Calendar Tools (CalDAV)
- `calendar_list_calendars` - List all calendars
- `calendar_list_events` - List events with date filtering
- `calendar_create_event` - Create new event
- `calendar_update_event` - Update existing event
- `calendar_delete_event` - Delete event
- `calendar_search_events` - Search events by text

### Contacts Tools (CardDAV)
- `contacts_list` - List all contacts
- `contacts_get` - Get specific contact
- `contacts_create` - Create new contact (name, phones, emails, addresses, organization, title)
- `contacts_update` - Update existing contact
- `contacts_delete` - Delete contact
- `contacts_search` - Search contacts by text

### Email Tools (IMAP/SMTP)
- `email_list_folders` - List mail folders
- `email_list_messages` - List messages in folder
- `email_get_message` - Get full message details
- `email_search` - Search messages by text
- `email_send` - Send email via SMTP
- `email_move` - Move message to folder
- `email_delete` - Delete or trash message
- `email_mark_read` - Mark message as read
- `email_mark_unread` - Mark message as unread

## Installation

### Prerequisites

- Python 3.10+
- iCloud account with App-Specific Password ([Generate here](https://appleid.apple.com/account/manage))

### Local Installation

```bash
# Clone repository
git clone <repository-url>
cd icloud-mcp

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials
```

### Docker Installation

```bash
# Clone repository
git clone <repository-url>
cd icloud-mcp

# Configure environment
cp .env.example .env
# Edit .env with your credentials

# Build and run with Docker Compose
docker-compose up -d
```

## Configuration

### Environment Variables

Create a `.env` file with the following variables:

```env
# iCloud Credentials (fallback if not in headers)
ICLOUD_EMAIL=your-email@icloud.com
ICLOUD_APP_SPECIFIC_PASSWORD=xxxx-xxxx-xxxx-xxxx

# iCloud Servers (optional, defaults to standard iCloud servers)
CALDAV_SERVER=https://caldav.icloud.com
CARDDAV_SERVER=https://contacts.icloud.com
IMAP_SERVER=imap.mail.me.com
SMTP_SERVER=smtp.mail.me.com

# Server Configuration
MCP_SERVER_PORT=8000
IMAP_PORT=993
SMTP_PORT=587
```

### Authentication

The server supports two authentication methods (checked in order):

1. **Request Headers** (recommended for multi-user scenarios):
   - `X-Apple-Email`: iCloud email address
   - `X-Apple-App-Specific-Password`: App-specific password

2. **Environment Variables** (fallback):
   - `ICLOUD_EMAIL`
   - `ICLOUD_APP_SPECIFIC_PASSWORD`

If credentials are not found in either location, the server returns a 401 error.

## Usage

### Local Usage (stdio transport)

```bash
# Using Python directly
python run.py

# Or using the module
python -m src.icloud_mcp.server
```

### Server Usage (HTTP/SSE transport)

```bash
# Using Python
python run.py --http --port 8000

# Using Docker Compose
docker-compose up
```

The server will be available at `http://localhost:8000`.

### Example: Using with MCP Client

```python
import requests

headers = {
    "X-Apple-Email": "your-email@icloud.com",
    "X-Apple-App-Specific-Password": "xxxx-xxxx-xxxx-xxxx"
}

# List calendars
response = requests.post(
    "http://localhost:8000/mcp/v1/tools/calendar_list_calendars",
    headers=headers,
    json={}
)
print(response.json())

# Create event
response = requests.post(
    "http://localhost:8000/mcp/v1/tools/calendar_create_event",
    headers=headers,
    json={
        "summary": "Team Meeting",
        "start": "2025-11-15T10:00:00",
        "end": "2025-11-15T11:00:00",
        "description": "Weekly sync meeting",
        "location": "Conference Room A"
    }
)
print(response.json())

# Send email
response = requests.post(
    "http://localhost:8000/mcp/v1/tools/email_send",
    headers=headers,
    json={
        "to": "recipient@example.com",
        "subject": "Hello from iCloud MCP",
        "body": "This is a test email sent via the MCP server."
    }
)
print(response.json())
```

## Architecture

### Stateless Design

The server is fully stateless:
- No sessions or state stored between requests
- Each request contains all necessary authentication information
- Connections to iCloud services are created per-request and closed immediately
- Perfect for horizontal scaling and serverless deployments

### Security Considerations

- Always use HTTPS in production when using HTTP transport
- Store App-Specific Passwords securely (use secret management tools)
- Consider using header-based authentication for multi-user scenarios
- Never commit `.env` file to version control

## Development

### Project Structure

```
icloud-mcp/
├── src/
│   └── icloud_mcp/
│       ├── __init__.py
│       ├── config.py       # Configuration management
│       ├── auth.py         # Authentication handling
│       ├── calendar.py     # CalDAV tools
│       ├── contacts.py     # CardDAV tools
│       ├── email.py        # IMAP/SMTP tools
│       └── server.py       # FastMCP server and tool registration
├── .env.example            # Example environment configuration
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── requirements.txt
├── run.py                  # Entry point script
└── README.md
```

### Running Tests

```bash
# Install development dependencies
pip install -e ".[dev]"

# Run tests (when added)
pytest
```

### Code Formatting

```bash
# Format code
black src/

# Lint code
ruff check src/
```

## Troubleshooting

### Authentication Errors

- Ensure you're using an **App-Specific Password**, not your regular iCloud password
- Generate one at: https://appleid.apple.com/account/manage
- Check that headers or environment variables are correctly set

### Connection Issues

- Verify your iCloud credentials are correct
- Check that you can access iCloud web interface
- Ensure firewall allows connections to iCloud servers
- For corporate networks, check proxy settings

### Calendar/Contact Operations

- Some operations may require specific iCloud subscription levels
- Calendar and contact IDs are URLs - store them for later operations
- Date formats must be ISO 8601 (e.g., "2025-11-15T10:00:00")

### Email Operations

- IMAP folder names are case-sensitive
- Message IDs are specific to folders
- Moving messages may take a moment to reflect in iCloud web interface

## License

MIT License - See LICENSE file for details

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Support

For issues and questions:
- Open an issue on GitHub
- Check existing issues for solutions
- Review iCloud API documentation

## Acknowledgments

Built with:
- [FastMCP](https://github.com/jlowin/fastmcp) - MCP server framework
- [caldav](https://github.com/python-caldav/caldav) - CalDAV/CardDAV library
- [IMAPClient](https://github.com/mjs/imapclient) - IMAP library
- [vobject](https://github.com/py-vobject/vobject) - vCard/iCalendar parsing
