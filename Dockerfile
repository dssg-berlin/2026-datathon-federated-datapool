# AWO MCP server as a container: MCP over streamable HTTP on http://<host>:8000/mcp.
#
#   docker build -t awo-mcp .
#   docker run --rm -p 8000:8000 awo-mcp
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what server.py needs at runtime (tests, benchmark and the CSV export stay out).
COPY server.py .
COPY src/ src/
COPY sparql_queries/ sparql_queries/
COPY data/einwohner_plz_de.csv data/

RUN useradd --create-home --uid 10001 mcp
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"MCP_PORT\"]}/health', timeout=4)"

CMD ["python", "server.py"]
