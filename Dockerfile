# Wisdomtooth MCP — containerized (API backend).
# NOTE: the claude-code (subscription) backend is NOT suited to Docker:
# it needs the `claude` CLI plus your OAuth login state on the host.
# Run the server directly on the host for subscription billing.
FROM python:3.12-slim
WORKDIR /app
# README.md and LICENSE are part of the package metadata (pyproject's `readme`
# and `license-files`); the build fails without them.
COPY pyproject.toml README.md LICENSE ./
COPY wisdomtooth ./wisdomtooth
RUN pip install --no-cache-dir .
# stdio by default. For a persistent server set ADVISOR_TRANSPORT=http and
# ADVISOR_HOST=0.0.0.0 -- which also requires ADVISOR_HTTP_TOKEN: the server
# refuses an unauthenticated bind beyond localhost.
ENTRYPOINT ["wisdomtooth-mcp"]
