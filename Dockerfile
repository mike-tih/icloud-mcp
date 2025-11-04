FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml ./
COPY src/ ./src/

# Install package
RUN pip install --no-cache-dir .

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app

USER app

# Cloud Run uses PORT env variable (default 8080, but we prefer 8000)
ENV PORT=8000
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Run in HTTP/SSE mode by default (for Cloud Run)
# Use 0.0.0.0 to listen on all interfaces (required for Cloud Run)
CMD sh -c "python -c \"from icloud_mcp.server import mcp; mcp.run(transport='sse', host='0.0.0.0', port=int('${PORT}'))\""
