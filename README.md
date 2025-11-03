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
- `email_get_messages` - Get multiple messages at once (bulk fetch)
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

# Install package in editable mode
pip install -e .

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
python -m icloud_mcp.server
```

### Server Usage (HTTP/SSE transport)

```bash
# Using Python
python run.py --http --port 8000

# Using Docker Compose
docker-compose up
```

The server will be available at `http://localhost:8000`.

## Integration with Claude Desktop

### Method 1: Local Execution with stdio (Recommended)

This method allows Claude Desktop to directly launch the MCP server as a subprocess.

**Step 1:** Install dependencies locally:
```bash
pip install -e .
```

**Step 2:** Create a `.env` file with your credentials:
```bash
cp .env.example .env
# Edit .env and add your iCloud credentials
```

**Step 3:** Find your Claude Desktop configuration file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

**Step 4:** Add this configuration (replace the path):

```json
{
  "mcpServers": {
    "icloud": {
      "command": "python",
      "args": ["/absolute/path/to/icloud-mcp/run.py"],
      "network": {
        "enabled": true,
        "allowedDomains": [
          "caldav.icloud.com",
          "contacts.icloud.com",
          "*.contacts.icloud.com",
          "imap.mail.me.com",
          "smtp.mail.me.com"
        ]
      }
    }
  }
}
```

**Important:** Replace `/absolute/path/to/icloud-mcp/` with the actual full path to your project directory.

**Example on macOS:**
```json
{
  "mcpServers": {
    "icloud": {
      "command": "python",
      "args": ["/Users/username/Projects/icloud-mcp/run.py"],
      "network": {
        "enabled": true,
        "allowedDomains": [
          "caldav.icloud.com",
          "contacts.icloud.com",
          "*.contacts.icloud.com",
          "imap.mail.me.com",
          "smtp.mail.me.com"
        ]
      }
    }
  }
}
```

**Note:** The `network.allowedDomains` configuration is **required** for contacts to work properly, as the server needs to access iCloud's CardDAV servers.

**Step 5:** Restart Claude Desktop completely (Quit and reopen)

### Method 2: Connect to Docker Server (HTTP Transport)

If you prefer running the server in Docker separately:

**Step 1:** Configure `.env` file and start Docker:
```bash
cp .env.example .env
# Edit .env with your credentials
docker-compose up -d
```

**Step 2:** Add this to Claude Desktop configuration:

```json
{
  "mcpServers": {
    "icloud": {
      "transport": {
        "type": "sse",
        "url": "http://localhost:8000/sse"
      }
    }
  }
}
```

Note: Credentials are read from the `.env` file in Docker, so no need to include them in the config.

**Step 3:** Restart Claude Desktop

### Verification

After restarting Claude Desktop:

1. Open Claude Desktop application
2. Look for the 🔨 (tools/hammer) icon in the bottom-right corner
3. You should see "icloud" server listed with green status
4. Try commands like:
   - "List my calendars"
   - "Show my contacts"
   - "Get my unread emails"

### Troubleshooting

**Server doesn't appear:**
- Check JSON syntax in config file (use a JSON validator)
- View logs: Help → Show Logs in Claude Desktop
- Verify the path to `run.py` is absolute (not relative)
- Ensure Python is in your PATH

**401 Authentication errors:**
- Ensure you're using an **App-Specific Password**, not your regular Apple password
- Generate one at: https://appleid.apple.com/account/manage
- Check `.env` file has correct credentials

**Contacts not working (empty results or errors):**
- Ensure you've added the `network.allowedDomains` configuration to Claude Desktop config
- The domains `contacts.icloud.com` and `*.contacts.icloud.com` must be in the allowed list
- Restart Claude Desktop after updating the config

**Tools fail with 500 errors:**
- Check server logs for details
- Verify iCloud credentials are valid
- Ensure network connectivity to iCloud servers

## Architecture

### Stateless Design

The server is fully stateless:
- No sessions or state stored between requests
- Each request contains all necessary authentication information
- Connections to iCloud services are created per-request and closed immediately
- Perfect for horizontal scaling and serverless deployments

### Technical Implementation

- **Calendar (CalDAV)**: Uses `caldav` library for standard CalDAV operations
- **Contacts (CardDAV)**: Direct HTTP/WebDAV implementation using `requests` with proper RFC 6352 CardDAV protocol
- **Email (IMAP/SMTP)**: Uses `imapclient` for IMAP and standard `smtplib` for SMTP

### Security Considerations

- Always use HTTPS in production when using HTTP transport
- Store App-Specific Passwords securely (use secret management tools)
- Consider using header-based authentication for multi-user scenarios
- Never commit `.env` file to version control
- Network access is restricted to allowed iCloud domains only

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
│       ├── contacts.py     # CardDAV tools (direct HTTP/WebDAV)
│       ├── email.py        # IMAP/SMTP tools
│       └── server.py       # FastMCP server and tool registration
├── .env.example            # Example environment configuration
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml          # Python project configuration and dependencies
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
- Verify network domains are in the allowed list for Claude Desktop

### Calendar/Contact Operations

- Some operations may require specific iCloud subscription levels
- Calendar and contact IDs are URLs - store them for later operations
- Date formats must be ISO 8601 (e.g., "2025-11-15T10:00:00")
- Contacts require proper network configuration in Claude Desktop

### Email Operations

- IMAP folder names are case-sensitive
- Message IDs are specific to folders
- Moving messages may take a moment to reflect in iCloud web interface

## Known Limitations

- Contact photos are not supported in the current implementation
- Some advanced CalDAV features may not be available
- Email attachments handling is limited in current version

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
- [caldav](https://github.com/python-caldav/caldav) - CalDAV library for calendar operations
- [requests](https://github.com/psf/requests) - HTTP library for CardDAV operations
- [IMAPClient](https://github.com/mjs/imapclient) - IMAP library
- [vobject](https://github.com/py-vobject/vobject) - vCard/iCalendar parsing
