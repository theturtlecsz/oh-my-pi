# NotebookLM Playbooks

Detailed workflow guides for common NotebookLM operations.

---

## Playbook A: Research Workflow

**Goal**: Discover, import, and query new sources on a topic.

### Steps

1. **Verify service health**

   ```bash
   notebooklm health
   ```

   If not running: `notebooklm service start`

2. **Select target notebook**

   ```bash
   notebooklm notebooks
   notebooklm select-notebook my-research
   ```

3. **Run fast research**

   ```bash
   notebooklm fast-research -n my-research "topic query" --import
   ```

   This searches the web and imports discovered sources.

4. **Verify imported sources**

   ```bash
   notebooklm list-sources -n my-research
   ```

5. **Ask questions from new sources**

   ```bash
   notebooklm ask -n my-research "What are the key findings?"
   ```

### When to use deep research

For complex, multi-faceted topics:

```bash
notebooklm deep-research -n my-research "Compare X vs Y" --edit-plan
```

Review the plan, then:

```bash
notebooklm approve-plan -n my-research
```

---

## Playbook B: Documentation Q&A

**Goal**: Query existing documentation for answers.

### Setup (one-time)

1. **Create a notebook in NotebookLM UI**
   - Go to https://notebooklm.google.com
   - Create notebook, add your documentation sources
   - Copy the notebook URL

2. **Add to library**

   ```bash
   notebooklm add-notebook \
     --name "project-docs" \
     --url "https://notebooklm.google.com/notebook/abc123" \
     --description "Project documentation" \
     --topics docs,api,architecture
   ```

### Query workflow

1. **Health check**

   ```bash
   notebooklm health
   ```

2. **Ask question**

   ```bash
   notebooklm ask -n project-docs "How does authentication work?"
   ```

3. **Follow-up questions**

   NotebookLM maintains context within a session:

   ```bash
   notebooklm ask -n project-docs "What about refresh tokens?"
   ```

### Expanding the knowledge base

```bash
# Add new documentation
notebooklm add-source -n project-docs "https://docs.example.com/new-feature"

# Refresh existing source
notebooklm refresh-source -n project-docs --indices 1
```

---

## Playbook C: Re-authentication

**Goal**: Fix authentication failures (401 errors).

### Symptoms

- `authenticated: false` in health check
- "Login required" errors
- Constant redirects to login page

### Steps

1. **Stop the service**

   ```bash
   notebooklm service stop
   ```

2. **Re-authenticate**

   ```bash
   notebooklm setup-auth
   ```

   A Chrome window opens. Log in to your Google account normally.

   **Important**: Complete the full login flow, then close the browser.

3. **Verify authentication**

   ```bash
   notebooklm health --deep-check
   ```

   Look for `authenticated: true`.

4. **Restart service**

   ```bash
   notebooklm service start
   ```

### Headless server authentication

For servers without a display:

1. **SSH with X11 forwarding**

   ```bash
   # From your local machine
   ssh -Y user@server
   ```

2. **Run setup-auth**

   ```bash
   notebooklm setup-auth
   ```

   The browser window appears on your local machine via X11.

3. **Alternative: Use existing Chrome profile**

   If you have Chrome logged in on the server:

   ```bash
   export CHROME_PROFILE_DIR=~/.config/chromium
   export NOTEBOOK_PROFILE_STRATEGY=single
   notebooklm service start
   ```

---

## Playbook D: Troubleshooting

### Service won't start

```bash
# Check what's using the port
lsof -i :3456

# Run in foreground for error messages
notebooklm service start --foreground

# Check for Chrome profile locks
ls -la ~/.local/share/notebooklm-mcp/chrome_profile/SingletonLock

# Clean up locks
notebooklm cleanup --confirm
notebooklm service start
```

### Operations timeout

```bash
# Check queue status
curl http://127.0.0.1:3456/api/queue

# Check sessions
notebooklm sessions

# Close idle sessions
notebooklm close-session -s <session-id>

# Increase timeout
notebooklm ask -n my-docs "question" --timeout 120

# Last resort: restart
notebooklm service restart
```

### Selectors fail (UI changed)

When Google updates NotebookLM's UI:

```bash
# Update to latest version
npm update -g notebooklm-client

# Debug with visible browser
notebooklm ask -n my-docs "test" --show-browser
```

### Chrome profile corruption

```bash
# Full cleanup (preserves library)
notebooklm cleanup --confirm --preserve-library

# Re-authenticate
notebooklm setup-auth

# Start fresh
notebooklm service start
```

---

## Playbook E: Multi-notebook workflow

**Goal**: Query different notebooks for different domains.

### Setup

```bash
# Add domain-specific notebooks
notebooklm add-notebook \
  --name "infra-docs" \
  --url "https://notebooklm.google.com/notebook/infra123" \
  --topics infrastructure,devops

notebooklm add-notebook \
  --name "api-docs" \
  --url "https://notebooklm.google.com/notebook/api456" \
  --topics api,backend

notebooklm add-notebook \
  --name "frontend-docs" \
  --url "https://notebooklm.google.com/notebook/front789" \
  --topics frontend,react
```

### Query by domain

```bash
# Infrastructure question
notebooklm ask -n infra-docs "How do I deploy to production?"

# API question
notebooklm ask -n api-docs "What's the authentication endpoint?"

# Frontend question
notebooklm ask -n frontend-docs "How do I implement routing?"
```

### Search notebooks by topic

```bash
notebooklm search-notebooks "api"
```

---

## Playbook F: Integration with local-memory

**Goal**: Use NotebookLM alongside local-memory for comprehensive Q&A.

### Strategy

- **local-memory**: Internal project knowledge, decisions, patterns
- **NotebookLM**: External documentation, research, grounded answers

### Workflow

1. **Query local memory first**

   ```bash
   lm ask "how does our EPG failover work?"
   ```

   This searches project-specific knowledge.

2. **Query NotebookLM for external docs**

   ```bash
   # Requires opt-in for privacy
   LM_ALLOW_REMOTE=1 lm ask "explain React context API" --notebook
   ```

   This routes to NotebookLM for external documentation.

3. **Combine insights**

   - Local memory provides project-specific context
   - NotebookLM provides authoritative documentation
   - Synthesize both for complete answers

### Domain-to-notebook mapping

Configure in `~/.local-memory/notebook-map.json`:

```json
{
  "infrastructure": "infra-docs",
  "frontend": "frontend-docs",
  "general": "general-docs"
}
```

Then `lm ask --notebook` automatically routes to the right notebook based on domain.

---

## Playbook G: Batch operations

**Goal**: Process multiple questions or sources efficiently.

### Batch questions

```bash
#!/bin/bash
NOTEBOOK="my-docs"
QUESTIONS=(
  "What is the main API endpoint?"
  "How do I authenticate?"
  "What are the rate limits?"
)

for q in "${QUESTIONS[@]}"; do
  echo "=== Q: $q ==="
  notebooklm ask -n "$NOTEBOOK" "$q"
  echo ""
done
```

### Batch source import

```bash
#!/bin/bash
NOTEBOOK="my-docs"
URLS=(
  "https://docs.example.com/api"
  "https://docs.example.com/auth"
  "https://docs.example.com/guides"
)

for url in "${URLS[@]}"; do
  echo "Adding: $url"
  notebooklm add-source -n "$NOTEBOOK" "$url"
done
```

### Using the HTTP API

For maximum efficiency, use the HTTP API directly:

```bash
# Parallel requests via curl + xargs
cat urls.txt | xargs -P 4 -I {} curl -X POST http://127.0.0.1:3456/api/sources \
  -H "Content-Type: application/json" \
  -d '{"source_type": "website", "content": "{}", "notebook": "my-docs"}'
```

---

## Playbook H: Monitoring & maintenance

### Daily checks

```bash
# Health check
notebooklm health

# Queue status
curl -s http://127.0.0.1:3456/api/queue | jq

# Session count
notebooklm sessions
```

### Weekly maintenance

```bash
# Clear old sessions
notebooklm sessions
# Close any that look stale

# Check research cache
notebooklm research-cache stats

# Clean if needed
notebooklm research-cache clear
```

### Monitoring metrics

```bash
# Prometheus metrics
curl http://127.0.0.1:3456/metrics
```

Key metrics:
- `notebooklm_requests_total` - total requests
- `notebooklm_queue_pending` - pending queue items
- `notebooklm_sessions_active` - active browser sessions
- `notebooklm_request_duration_seconds` - request latency
