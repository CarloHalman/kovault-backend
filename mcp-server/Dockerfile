# kovault-mcp — the MCP server. Holds the DB credentials; exposes the HTTP endpoint the
# plugin connects to. The embedding model runs in its own (already-running) container and
# is reached by URL from the settings table.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY kovault_mcp ./kovault_mcp
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["python", "-m", "kovault_mcp.main"]
