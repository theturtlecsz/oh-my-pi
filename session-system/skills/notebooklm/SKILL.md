---
name: notebooklm
description: "Query NotebookLM notebooks for zero-hallucination answers from your knowledge bases. Health check first, ask questions via notebook ID, manage sources, run research. Service must be running."
---

# NotebookLM Skill

**Purpose**: Doc-grounded Q&A and service health. **NOT** a golden path orchestrator.

This skill provides access to NotebookLM for:
1) Zero-hallucination answers from uploaded documents via Gemini
2) Ad-hoc research and document queries
3) Service health diagnostics

> Stage0 owns Tier2 orchestration for `/speckit.auto`. This skill is for **ad-hoc doc work and debugging**.

---

## Non-negotiables

- **Health check first** - always run `notebooklm doctor` or `curl localhost:3456/health/ready`
- **Explicit notebook required** - every query needs `-n <notebook-id>`. FAIL CLOSED when ambiguous
- **Service must be running** - `notebooklm service start`
- **Authentication required** - see re-auth workflow if 401 errors
- **Never send secrets** - NotebookLM is a Google service. Never upload credentials, tokens, or sensitive data

---

## Quick Reference

```bash
# Health check (ALWAYS FIRST)
notebooklm doctor                 # comprehensive diagnostics
# OR
curl localhost:3456/health/ready  # quick readiness check

# List notebooks
notebooklm notebooks

# Ask a question
notebooklm ask -n <notebook-id> "your question"

# Add source to notebook
notebooklm add-source -n <notebook-id> "https://url.com"

# Fast research (web search + import)
notebooklm fast-research -n <notebook-id> "query" --import
```

---

## Service Lifecycle

### Systemd (recommended for persistent service)

```bash
# Install systemd user service (one-time)
notebooklm service install

# Start via systemd
systemctl --user start notebooklm

# Check status
systemctl --user status notebooklm

# Stop
systemctl --user stop notebooklm

# View logs
journalctl --user -u notebooklm -f

# Enable auto-start on login
loginctl enable-linger $USER
```

### Manual service (for debugging)

```bash
# Start daemon
notebooklm service start

# Check status
notebooklm service status

# Stop
notebooklm service stop

# Restart
notebooklm service restart
```

Service runs on `http://127.0.0.1:3456`.

---

## Primary Workflow: Ask Questions

### 1. Verify health

```bash
notebooklm health
```

Expected output includes `status: ok`.

### 2. List available notebooks

```bash
notebooklm notebooks
```

Returns notebook IDs, names, and URLs.

### 3. Ask your question

```bash
notebooklm ask -n my-docs "How does authentication work?"
```

**Options:**
- `--json` - JSON output for automation
- `--show-browser` - debug UI issues

---

## Source Management

### List sources in a notebook

```bash
notebooklm list-sources -n my-docs
```

### Add a website

```bash
notebooklm add-source -n my-docs "https://docs.example.com"
```

### Add a local file

```bash
notebooklm add-source -n my-docs --type file ./document.pdf
```

### Add text content

```bash
notebooklm add-source -n my-docs --type text "My notes..."
```

### Add YouTube video

```bash
notebooklm add-source -n my-docs --type youtube "https://youtube.com/watch?v=..."
```

---

## Research Workflow

### Fast research (quick web search)

```bash
notebooklm fast-research -n my-docs "React hooks best practices"
```

### Fast research with auto-import

```bash
notebooklm fast-research -n my-docs "query" --import
```

### Deep research (multi-step autonomous)

```bash
notebooklm deep-research -n my-docs "Compare X vs Y" --timeout 180
```

### Import research results

```bash
notebooklm import-results -n my-docs
```

---

## Error Recovery

### Service not running

```bash
# Check status
notebooklm service status

# Start if needed
notebooklm service start
```

### Authentication failure (401)

```bash
# Re-authenticate
notebooklm setup-auth

# Then restart service
notebooklm service restart
```

### Timeout errors

```bash
# Increase timeout
notebooklm ask -n my-docs "question" --timeout 120

# Or restart service
notebooklm service restart
```

### Chrome profile lock

```bash
# Use service mode (recommended)
notebooklm service start

# Or clean up locks
notebooklm cleanup --confirm
```

---

## Integration with local-memory

Use NotebookLM for questions requiring **external research** or **document-grounded answers**:

```bash
# Local memory first (internal knowledge)
lm ask "how does our EPG system work?"

# NotebookLM for external docs (requires opt-in)
LM_ALLOW_REMOTE=1 lm ask "explain React hooks" --notebook
```

The `--notebook` flag in `lm ask` routes to NotebookLM via domain mapping.

---

## Environment

- `NOTEBOOKLM_SERVICE_HOST` (default `127.0.0.1`) - service host
- `NOTEBOOKLM_SERVICE_PORT` (default `3456`) - service port
- `LM_ALLOW_REMOTE` - required for `lm ask --notebook` privacy gate

---

## References

- [API.md](./API.md) - Full HTTP API reference (40 endpoints)
- [reference.md](./reference.md) - Full CLI command reference
- [playbooks.md](./playbooks.md) - Detailed workflow guides
- `~/notebooklm-client/README.md` - Service documentation
- `~/notebooklm-client/docs/handbook/` - Complete handbook
