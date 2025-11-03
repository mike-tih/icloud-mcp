FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY src/ ./src/

# Install package and dependencies
RUN pip install --no-cache-dir -e .

# Set Python path
ENV PYTHONPATH=/app

# Expose port for HTTP transport
EXPOSE 8000

# Default to stdio transport, can be overridden
CMD ["python", "-m", "icloud_mcp.server"]
