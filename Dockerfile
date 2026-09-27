# Wisdomtooth MCP, containerized. The container uses the API backend:
#   docker run -i --rm -e ANTHROPIC_API_KEY ghcr.io/h4l0b3nd3r/wisdomtooth-mcp
# The Claude Code backend and CLI advisors need their CLIs and sign-ins on the
# host; run the server there for those.
FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/H4l0B3nd3r/Wisdomtooth-MCP" \
      org.opencontainers.image.description="MCP server that lets coding agents ask a frontier model for advice" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV ADVISOR_BACKEND=api PYTHONUNBUFFERED=1
WORKDIR /app
# README.md and LICENSE are part of the package metadata (pyproject's `readme`
# and `license-files`); the build fails without them.
COPY pyproject.toml README.md LICENSE ./
COPY wisdomtooth ./wisdomtooth
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 wisdomtooth
# Not root: state (~/.wisdomtooth) lives in this user's home.
USER wisdomtooth
# stdio by default. For a persistent server set ADVISOR_TRANSPORT=http and
# ADVISOR_HOST=0.0.0.0 -- which also requires ADVISOR_HTTP_TOKEN: the server
# refuses an unauthenticated bind beyond localhost.
ENTRYPOINT ["wisdomtooth-mcp"]
