# NotebookLM CLI Reference

Complete command reference for the NotebookLM service CLI.

---

## Global Options

| Option | Description |
|--------|-------------|
| `--notebook, -n <id\|url>` | Notebook ID from library or full URL |
| `--show-browser` | Show browser window (default: headless) |
| `--json` | Output as JSON |
| `--verbose, -v` | Detailed output |
| `--direct` | Force direct browser mode (skip service) |
| `--timeout <seconds>` | Operation timeout |

---

## Service Commands

### `service start`

Start the service daemon.

```bash
notebooklm service start
notebooklm service start --port 8080
notebooklm service start --foreground  # debug mode
```

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | 3456 | Port to listen on |
| `--host` | 127.0.0.1 | Host to bind to |
| `--foreground` | false | Run in foreground |

### `service stop`

Stop the running service.

```bash
notebooklm service stop
```

### `service status`

Check if service is running.

```bash
notebooklm service status
```

### `service restart`

Restart the service.

```bash
notebooklm service restart
```

### `service install`

Install as systemd user service (Linux).

```bash
notebooklm service install
```

### `service uninstall`

Remove systemd user service.

```bash
notebooklm service uninstall
```

---

## Health & Status

### `doctor`

Comprehensive service diagnostics. Reports service, auth, queue, and library status.

```bash
notebooklm doctor                  # Fast diagnostics
notebooklm doctor --deep-check     # With browser-based auth validation
notebooklm doctor --json           # JSON output
```

**Exit Codes:**
- `0`: Ready - service is healthy
- `1`: Not ready - fix issues shown

### `health`

Basic health check.

```bash
notebooklm health
notebooklm health --deep-check  # includes auth verification
```

### `stats`

Library statistics.

```bash
notebooklm stats
```

---

## Notebook Commands

### `notebooks`

List all notebooks in library.

```bash
notebooklm notebooks
notebooklm notebooks --json
```

### `add-notebook`

Add a notebook to library.

```bash
notebooklm add-notebook \
  --name "React Docs" \
  --url "https://notebooklm.google.com/notebook/abc123" \
  --description "React 18 documentation" \
  --topics react,frontend
```

| Option | Required | Description |
|--------|----------|-------------|
| `--name` | Yes | Display name |
| `--url` | Yes | NotebookLM URL |
| `--description` | No | Description |
| `--topics` | No | Comma-separated tags |

### `get-notebook`

Get notebook details.

```bash
notebooklm get-notebook my-notebook-id
```

### `select-notebook`

Set active default notebook.

```bash
notebooklm select-notebook my-notebook-id
```

### `search-notebooks`

Search notebooks by name/topic.

```bash
notebooklm search-notebooks "react"
```

### `update-notebook`

Update notebook metadata.

```bash
notebooklm update-notebook my-notebook-id \
  --description "Updated description" \
  --topics new,tags
```

### `remove-notebook`

Remove from library (doesn't delete in NotebookLM).

```bash
notebooklm remove-notebook my-notebook-id
```

### `create-notebook`

Create a new notebook in NotebookLM.

```bash
notebooklm create-notebook --name "New Research"
```

---

## Ask Questions

### `ask`

Ask a question to a notebook.

```bash
notebooklm ask -n my-docs "How do I implement caching?"
notebooklm ask -n my-docs "Explain the API" --json
notebooklm ask -n my-docs "Debug this" --show-browser
```

| Option | Description |
|--------|-------------|
| `-n, --notebook` | Target notebook (required) |
| `--json` | JSON output |
| `--show-browser` | Visible browser |
| `--timeout` | Seconds to wait |

---

## Source Management

### `list-sources`

List sources in a notebook.

```bash
notebooklm list-sources -n my-docs
```

### `get-sources`

Get sources with selection state.

```bash
notebooklm get-sources -n my-docs
```

### `add-source`

Add a source to a notebook.

```bash
# Website (default)
notebooklm add-source -n my-docs "https://example.com/docs"

# YouTube video
notebooklm add-source -n my-docs --type youtube "https://youtube.com/watch?v=abc"

# Local file
notebooklm add-source -n my-docs --type file ./document.pdf

# Text content
notebooklm add-source -n my-docs --type text "My notes about the topic..."

# Google Docs
notebooklm add-source -n my-docs --type google_docs "https://docs.google.com/..."
```

| Type | Description |
|------|-------------|
| `website` | Web page (default) |
| `youtube` | YouTube video |
| `file` | Local file (PDF, TXT, etc.) |
| `text` | Raw text content |
| `google_docs` | Google Docs URL |

### `add-sources`

Add multiple sources at once.

```bash
notebooklm add-sources -n my-docs "https://url1.com" "https://url2.com"
```

### `select-sources`

Select specific sources by index (1-indexed).

```bash
notebooklm select-sources -n my-docs --indices 1,2,3
```

### `deselect-all`

Deselect all sources.

```bash
notebooklm deselect-all -n my-docs
```

### `delete-source`

Delete a source by index.

```bash
notebooklm delete-source -n my-docs --indices 3
```

### `refresh-source`

Refresh/sync a source.

```bash
notebooklm refresh-source -n my-docs --indices 1
```

---

## Research Commands

### `fast-research`

Quick parallel web search.

```bash
notebooklm fast-research -n my-docs "TypeScript generics tutorial"
notebooklm fast-research -n my-docs "React patterns" --import
notebooklm fast-research -n my-docs "query" --from-cache
notebooklm fast-research -n my-docs "React vs Vue" --auto-template
```

| Option | Description |
|--------|-------------|
| `--import` | Auto-import results |
| `--from-cache` | Use cached results if available |
| `--auto-template` | Auto-select best research template |

### `deep-research`

Multi-step autonomous research.

```bash
notebooklm deep-research -n my-docs "Compare React vs Vue" --timeout 180
notebooklm deep-research -n my-docs "AI trends" --edit-plan
notebooklm deep-research -n my-docs "topic" --template comparison
```

| Option | Description |
|--------|-------------|
| `--timeout` | Seconds (default: 120) |
| `--edit-plan` | Pause for plan review |
| `--template` | Use specific research template |

### `research-status`

Check research progress.

```bash
notebooklm research-status -n my-docs
```

### `research-results`

Get completed research results.

```bash
notebooklm research-results -n my-docs
notebooklm research-results -n my-docs --json
```

### `import-results`

Import research results to notebook.

```bash
notebooklm import-results -n my-docs
```

### `approve-plan`

Approve a pending research plan.

```bash
notebooklm approve-plan -n my-docs
```

### `edit-plan`

Edit research plan from file.

```bash
notebooklm edit-plan -n my-docs --plan-file plan.txt --approve
```

### `research-history`

View research history.

```bash
notebooklm research-history
notebooklm research-history --all --verbose
```

### `research-cache`

Manage research cache.

```bash
notebooklm research-cache stats
notebooklm research-cache clear
notebooklm research-cache show <entry-id>
```

### `research-templates`

Manage research templates.

```bash
notebooklm research-templates list
notebooklm research-templates show comparison
```

### `analyze-query`

Analyze query for template suggestions.

```bash
notebooklm analyze-query "React vs Vue comparison"
```

---

## Session Management

### `sessions`

List active browser sessions.

```bash
notebooklm sessions
```

### `close-session`

Close a specific session.

```bash
notebooklm close-session -s session-123
```

### `reset-session`

Reset a session state.

```bash
notebooklm reset-session -s session-123
```

---

## Authentication

### `setup-auth`

Interactive authentication setup.

```bash
notebooklm setup-auth
```

Opens Chrome for Google login. Close browser when done.

### `re-auth`

Re-authenticate (switch accounts).

```bash
notebooklm re-auth
```

---

## Cleanup & Maintenance

### `cleanup`

Clean up all data.

```bash
notebooklm cleanup                     # preview
notebooklm cleanup --confirm           # actually delete
notebooklm cleanup --confirm --preserve-library  # keep notebooks
```

---

## HTTP API Endpoints

When service is running, these endpoints are available:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Full health check |
| `/health/live` | GET | Liveness probe |
| `/health/ready` | GET | Readiness probe |
| `/api/notebooks` | GET | List notebooks |
| `/api/notebooks` | POST | Add notebook |
| `/api/notebooks/:id` | GET | Get notebook |
| `/api/notebooks/:id` | DELETE | Remove notebook |
| `/api/ask` | POST | Ask question |
| `/api/sources` | GET | List sources |
| `/api/sources` | POST | Add source |
| `/api/research/fast` | POST | Fast research |
| `/api/research/deep` | POST | Deep research |
| `/api/sessions` | GET | List sessions |

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Service not running |
| 3 | Authentication required |
| 4 | Notebook not found |
| 5 | Timeout |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NOTEBOOKLM_SERVICE_HOST` | 127.0.0.1 | Service host |
| `NOTEBOOKLM_SERVICE_PORT` | 3456 | Service port |
| `HEADLESS` | true | Run Chrome headless |
| `BROWSER_TIMEOUT` | 30000 | Operation timeout (ms) |
| `MAX_SESSIONS` | 10 | Max concurrent sessions |
| `CHROME_PROFILE_DIR` | ~/.local/share/notebooklm-mcp/chrome_profile | Chrome profile |
