"""Configuration management for iCloud MCP server."""

import os
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Configuration for iCloud MCP server (stateless, loaded from environment)."""

    # Default iCloud servers
    CALDAV_SERVER: str = os.getenv("CALDAV_SERVER", "https://caldav.icloud.com")
    CARDDAV_SERVER: str = os.getenv("CARDDAV_SERVER", "https://contacts.icloud.com")
    IMAP_SERVER: str = os.getenv("IMAP_SERVER", "imap.mail.me.com")
    SMTP_SERVER: str = os.getenv("SMTP_SERVER", "smtp.mail.me.com")

    # Ports
    MCP_SERVER_PORT: int = int(os.getenv("MCP_SERVER_PORT", "8000"))
    IMAP_PORT: int = int(os.getenv("IMAP_PORT", "993"))
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))

    # Fallback credentials (if not provided in headers)
    FALLBACK_EMAIL: Optional[str] = os.getenv("ICLOUD_EMAIL")
    FALLBACK_PASSWORD: Optional[str] = os.getenv("ICLOUD_APP_SPECIFIC_PASSWORD")


config = Config()
