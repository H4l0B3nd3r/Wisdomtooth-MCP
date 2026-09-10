# Claude Advisor MCP — containerized (API backend).
# NOTE: the claude-code (subscription) backend is NOT suited to Docker:
# it needs the `claude` CLI plus your OAuth login state on the host.
# Run the server directly on the host for subscription billing.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY claude_advisor ./claude_advisor
RUN pip install --no-cache-dir .
# stdio by default; set ADVISOR_TRANSPORT=http for a persistent server
ENTRYPOINT ["claude-advisor-mcp"]
