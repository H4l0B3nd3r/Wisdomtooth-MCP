# Security policy

## Reporting a vulnerability

Please report security problems privately, through GitHub's
[private vulnerability reporting](https://github.com/H4l0B3nd3r/Wisdomtooth-MCP/security/advisories/new),
not in a public issue. Include the version (`wisdomtooth-mcp --version`), your
operating system, and the steps to reproduce.

Only the latest release receives fixes.

## Scope

Wisdomtooth runs on your machine with your credentials, so these matter most:

- anything that lets text from the calling agent run commands or read files
  outside `ADVISOR_FILE_ROOTS`;
- credentials (`~/.wisdomtooth/credentials.json`, `advisors.json`, API keys)
  leaking into tool results, transcripts, logs or outbound requests;
- the HTTP transport accepting requests without the configured token.

Secret redaction of outbound context is a best-effort safeguard; a secret
format it does not recognise is a bug worth reporting, but not a
vulnerability.
