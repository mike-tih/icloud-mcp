"""Authentication management for iCloud MCP server."""

from typing import Tuple, Optional
from fastmcp import Context
from .config import config


class AuthenticationError(Exception):
    """Raised when authentication fails."""
    pass


def get_credentials(context: Context) -> Tuple[str, str]:
    """
    Extract iCloud credentials from request headers or environment variables.

    Priority:
    1. Request headers: X-Apple-Email and X-Apple-App-Specific-Password
    2. Environment variables: ICLOUD_EMAIL and ICLOUD_APP_SPECIFIC_PASSWORD

    Args:
        context: FastMCP context containing request metadata

    Returns:
        Tuple of (email, app_specific_password)

    Raises:
        AuthenticationError: If credentials are not found
    """
    # Try to get credentials from headers
    headers = getattr(context, "headers", {}) or {}

    email: Optional[str] = headers.get("x-apple-email") or headers.get("X-Apple-Email")
    password: Optional[str] = headers.get("x-apple-app-specific-password") or headers.get("X-Apple-App-Specific-Password")

    # Fallback to environment variables
    if not email:
        email = config.FALLBACK_EMAIL
    if not password:
        password = config.FALLBACK_PASSWORD

    # Validate credentials
    if not email or not password:
        raise AuthenticationError(
            "Authentication required. Provide credentials via headers "
            "(X-Apple-Email, X-Apple-App-Specific-Password) or environment variables "
            "(ICLOUD_EMAIL, ICLOUD_APP_SPECIFIC_PASSWORD)"
        )

    return email, password


def require_auth(context: Context) -> Tuple[str, str]:
    """
    Decorator-friendly authentication check.

    Args:
        context: FastMCP context

    Returns:
        Tuple of (email, app_specific_password)

    Raises:
        AuthenticationError: If authentication fails
    """
    return get_credentials(context)
