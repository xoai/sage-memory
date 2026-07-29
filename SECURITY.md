# Security Policy

## Threat model

sage-memory is a **local-first** MCP memory server. The trust boundary
is the machine: per-project SQLite databases live in `.sage-memory/`
and the server is designed to run as a subprocess of a local MCP
client (stdio) or bound to loopback.

Three surfaces exist, in increasing order of exposure:

1. **stdio (default).** A pipe to a local parent process. No network
   surface; inherits the parent process's privileges. No auth — the
   OS process boundary is the control.
2. **sse / http on loopback.** Zero-config for local tools and team
   setups on one machine. A Host-header allowlist (loopback spellings
   plus explicit `--allowed-host`) and `Origin` validation defend
   against DNS-rebinding and browser cross-origin drives from malicious
   web pages. Set `--token` for defense in depth on shared machines.
3. **sse / http on a non-loopback bind.** The server **refuses to
   start without a bearer token** (`--token` or `SAGE_MEMORY_TOKEN`).
   Every request must carry `Authorization: Bearer <token>` (compared
   with `hmac.compare_digest`). Deploy behind a TLS-terminating
   reverse proxy for anything beyond a trusted LAN — the token is
   sent in cleartext over plain HTTP.

### What an agent under prompt injection can and cannot do

An MCP client (an AI agent) can call any of the `sage_memory_*`
tools. Within that surface:

- `sage_memory_set_project` is **scoped**: it accepts only paths
  inside the detected project root subtree (or the launch directory
  when no project markers exist) plus roots explicitly listed in
  `SAGE_ALLOWED_ROOTS` (os.pathsep-separated). `~/.ssh`, `~/.gnupg`,
  `~/.aws`, and `/etc` are always denied, and the home directory
  itself was already rejected. Containment uses resolved-path
  `Path.is_relative_to`, so sibling-prefix tricks
  (`/tmp/proj-secret` vs `/tmp/proj`) fail.
- Store/update/delete affect only the active project DB (and the
  global DB by explicit scope). Graph and search are read paths over
  the same DBs.
- There is no shell-out, no file-path argument other than
  `set_project`, and no network egress in the required path. Optional
  LLM/embedder calls (env-key gated) are the only outbound traffic.

Residual risk: a prompt-injected agent can read and corrupt memories
in projects the operator has allowed. Treat memory content as
untrusted input when it flows back into agent context.

### Credentials

OAuth tokens for the optional dedup worker are stored in
`~/.sage-memory/auth.json` (mode 0600, directory 0700, atomic
temp+chmod+replace writes). API keys are environment-only; they are
never written to config files and are stripped from `config get_all()`
output.

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please **do not** open a public issue. Email the maintainer via the
contact on the GitHub profile at <https://github.com/xoai>, or use
GitHub's private vulnerability reporting ("Report a vulnerability"
on the Security tab of `xoai/sage-memory`).

Include: affected version, reproduction steps, and the surface
(stdio / sse / http / Docker). You can expect an acknowledgement
within 72 hours and a fix or mitigation plan within 14 days for
confirmed issues.
