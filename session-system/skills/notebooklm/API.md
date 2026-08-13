# NotebookLM Service API Reference

**Base URL**: `http://127.0.0.1:3456`
**Version**: 2.1.0
**Content-Type**: `application/json`

---

## Quick Reference

| Category | Endpoint | Method | Browser Required |
|----------|----------|--------|------------------|
| Health | `/health` | GET | No |
| Health | `/health/live` | GET | No |
| Health | `/health/ready` | GET | No (optional deep) |
| Notebooks | `/api/notebooks` | GET | No |
| Notebooks | `/api/notebooks` | POST | No |
| Notebooks | `/api/notebooks/create` | POST | **Yes** |
| Notebooks | `/api/notebooks/search` | GET | No |
| Notebooks | `/api/notebooks/:id` | GET | No |
| Notebooks | `/api/notebooks/:id` | DELETE | No |
| Notebooks | `/api/notebooks/:id` | PATCH | No |
| Notebooks | `/api/notebooks/:id/select` | PUT | No |
| Questions | `/api/ask` | POST | **Yes** |
| Sources | `/api/sources` | GET | **Yes** |
| Sources | `/api/sources` | POST | **Yes** |
| Sources | `/api/sources/bulk` | POST | **Yes** |
| Sources | `/api/sources/selected` | GET | **Yes** |
| Sources | `/api/sources/select` | POST | **Yes** |
| Sources | `/api/sources/deselect-all` | POST | **Yes** |
| Sources | `/api/sources/:index` | DELETE | **Yes** |
| Sources | `/api/sources/:index/refresh` | POST | **Yes** |
| Research | `/api/research/fast` | POST | **Yes** |
| Research | `/api/research/deep` | POST | **Yes** |
| Research | `/api/research/status` | GET | **Yes** |
| Research | `/api/research/results` | GET | **Yes** |
| Research | `/api/research/import` | POST | **Yes** |
| Research | `/api/research/approve` | POST | **Yes** |
| Research | `/api/research/plan` | PUT | **Yes** |
| Research | `/api/research/history` | GET | No |
| Research | `/api/research/cache` | GET | No |
| Research | `/api/research/cache` | DELETE | No |
| Research | `/api/research/templates` | GET | No |
| Research | `/api/research/analyze` | POST | No |
| Sessions | `/api/sessions` | GET | No |
| Sessions | `/api/sessions/:id` | DELETE | **Yes** |
| Sessions | `/api/sessions/:id/reset` | POST | **Yes** |
| System | `/api/stats` | GET | No |
| System | `/api/cleanup` | POST | No |
| System | `/api/queue` | GET | No |
| Metrics | `/metrics` | GET | No |

---

## Response Format

All responses follow this structure:

```json
{
  "success": true,
  "data": { ... }
}
```

Or on error:

```json
{
  "success": false,
  "error": "Error message"
}
```

**Exception**: Health endpoints return raw objects without the `success` wrapper.

---

## Health & Status

### GET /health

Comprehensive service health check.

**Response**:
```json
{
  "status": "ok",
  "service": "notebooklm",
  "service_version": "2.1.0",
  "api_version": "v1",
  "uptime": 3600.5,
  "queue": {
    "pending": 0,
    "processing": false,
    "totalProcessed": 42,
    "totalFailed": 1
  },
  "sessions": {
    "active_sessions": 2,
    "max_sessions": 10
  },
  "context": {
    "healthy": true,
    "memory": {
      "heapUsed": 52428800,
      "heapTotal": 104857600,
      "percentUsed": 50
    }
  }
}
```

### GET /health/live

Kubernetes liveness probe.

**Response**:
```json
{
  "status": "alive",
  "timestamp": "2025-12-25T02:55:00.000Z",
  "uptime": 3600.5
}
```

### GET /health/ready

Kubernetes readiness probe. Returns 200 if ready, 503 if not.

**Query Parameters**:
| Name | Type | Description |
|------|------|-------------|
| `deep` | boolean | If `true`, performs browser-based auth validation (slower, ~5-10s) |

**Response (Ready)**:
```json
{
  "status": "ready",
  "ready": true,
  "service_version": "2.1.0",
  "api_version": "v1",
  "timestamp": "2025-12-25T02:55:00.000Z",
  "deepCheck": false,
  "checks": {
    "authenticated": true,
    "notRecovering": true,
    "queueAvailable": true,
    "memoryOk": true
  },
  "details": {
    "authReason": "Chrome profile and auth state present",
    "recovering": false,
    "queuePending": 0,
    "queueProcessing": false,
    "memoryPercent": 50
  }
}
```

**Response (Not Ready - 503)**:
```json
{
  "status": "not_ready",
  "ready": false,
  "errorCode": "AUTH_REQUIRED",
  "actionRequired": "Run 'notebooklm setup-auth' to authenticate",
  ...
}
```

**Error Codes**:
| Code | Meaning | Action |
|------|---------|--------|
| `AUTH_REQUIRED` | Not authenticated | Run `notebooklm setup-auth` |
| `QUEUE_OVERLOADED` | Too many pending requests | Wait or increase capacity |
| `RECOVERING` | Service recovering from crash | Wait for recovery |

---

## Notebooks

### GET /api/notebooks

List all notebooks in the local library.

**Response**:
```json
{
  "success": true,
  "data": {
    "notebooks": [
      {
        "id": "my-notebook",
        "url": "https://notebooklm.google.com/notebook/abc123",
        "name": "My Notebook",
        "description": "Research on AI",
        "topics": ["ai", "research"],
        "added_at": "2025-12-25T00:00:00.000Z",
        "last_used": "2025-12-25T02:00:00.000Z",
        "use_count": 15,
        "tags": []
      }
    ]
  }
}
```

### POST /api/notebooks

Add an existing NotebookLM notebook to the local library.

**Request Body**:
```json
{
  "url": "https://notebooklm.google.com/notebook/abc123",
  "name": "My Notebook",
  "description": "Optional description",
  "topics": ["topic1", "topic2"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | **Yes** | NotebookLM notebook URL |
| `name` | string | **Yes** | Display name (becomes ID) |
| `description` | string | No | Description |
| `topics` | string[] | No | Topic tags (default: `["general"]`) |

**Response**:
```json
{
  "success": true,
  "data": {
    "notebook": { ... }
  }
}
```

### POST /api/notebooks/create

Create a new notebook in NotebookLM (browser required).

**Request Body**:
```json
{
  "name": "New Notebook",
  "description": "Optional description",
  "topics": ["topic1"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | No | Name (auto-added to library if provided) |
| `description` | string | No | Description |
| `topics` | string[] | No | Topic tags |

**Response**:
```json
{
  "success": true,
  "data": {
    "notebookUrl": "https://notebooklm.google.com/notebook/new-id",
    "notebook": { ... }
  }
}
```

### GET /api/notebooks/search

Search notebooks in library.

**Query Parameters**:
| Name | Type | Description |
|------|------|-------------|
| `q` | string | Search query (matches name, description, topics) |

**Response**:
```json
{
  "success": true,
  "data": {
    "notebooks": [...],
    "query": "ai research"
  }
}
```

### GET /api/notebooks/:id

Get notebook by ID.

**Response**:
```json
{
  "success": true,
  "data": {
    "notebook": { ... }
  }
}
```

### DELETE /api/notebooks/:id

Remove notebook from library (does not delete from NotebookLM).

**Response**:
```json
{
  "success": true,
  "data": {
    "removed": true
  }
}
```

### PATCH /api/notebooks/:id

Update notebook metadata.

**Request Body**:
```json
{
  "name": "Updated Name",
  "description": "Updated description",
  "topics": ["new-topic"]
}
```

At least one field required.

### PUT /api/notebooks/:id/select

Set notebook as active (default for operations without explicit notebook).

**Response**:
```json
{
  "success": true,
  "data": {
    "notebook": { ... }
  }
}
```

---

## Questions

### POST /api/ask

Ask a question to a notebook (browser required).

**Request Body**:
```json
{
  "question": "What are the key findings?",
  "notebook": "my-notebook",
  "session_id": "optional-session-id"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `question` | string | **Yes** | Question text |
| `notebook` | string | No | Notebook ID, URL, or omit for active notebook |
| `session_id` | string | No | Reuse existing browser session |

**Notebook Resolution**: Accepts `notebook`, `notebook_url`, or `notebook_id` fields.

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "sessionId": "session-abc123",
    "notebookUrl": "https://notebooklm.google.com/notebook/...",
    "answer": "The key findings include..."
  }
}
```

---

## Sources

### Source Types

| Type | Content | Example |
|------|---------|---------|
| `website` | URL | `https://example.com/article` |
| `youtube` | YouTube URL | `https://youtube.com/watch?v=...` |
| `text` | Plain text | `"This is my content..."` |
| `file` | File path | `/path/to/document.pdf` |
| `google_docs` | Google Docs URL | `https://docs.google.com/...` |

### GET /api/sources

List sources in a notebook.

**Query Parameters**:
| Name | Type | Description |
|------|------|-------------|
| `notebook` | string | Notebook ID/URL (optional if active set) |

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "sources": [
      {
        "index": 0,
        "title": "Example Article",
        "type": "website",
        "status": "ready",
        "wordCount": 2500,
        "selected": true
      }
    ],
    "sourceCount": 3
  }
}
```

### POST /api/sources

Add a source to a notebook.

**Request Body**:
```json
{
  "source_type": "website",
  "content": "https://example.com/article",
  "notebook": "my-notebook"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_type` | string | **Yes** | One of: `website`, `youtube`, `text`, `file`, `google_docs` |
| `content` | string | **Yes** | URL, text, or file path |
| `notebook` | string | No | Notebook ID/URL |

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "sourceType": "website",
    "content": "https://example.com/article",
    "status": "processing"
  }
}
```

### POST /api/sources/bulk

Add multiple sources at once.

**Request Body**:
```json
{
  "sources": [
    { "source_type": "website", "content": "https://example.com/1" },
    { "source_type": "youtube", "content": "https://youtube.com/watch?v=abc" }
  ],
  "notebook": "my-notebook"
}
```

**Response**:
```json
{
  "success": true,
  "data": {
    "total": 2,
    "succeeded": 2,
    "failed": 0,
    "results": [
      { "success": true },
      { "success": true }
    ]
  }
}
```

### GET /api/sources/selected

Get currently selected sources.

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "totalSources": 5,
    "selectedCount": 3,
    "selectedIndices": [0, 2, 4],
    "sources": [...]
  }
}
```

### POST /api/sources/select

Select sources by index.

**Request Body**:
```json
{
  "indices": [0, 2, 4],
  "notebook": "my-notebook"
}
```

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "selected": [0, 2, 4],
    "failed": []
  }
}
```

### POST /api/sources/deselect-all

Deselect all sources.

**Request Body**:
```json
{
  "notebook": "my-notebook"
}
```

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "deselectedCount": 5
  }
}
```

### DELETE /api/sources/:index

Delete a source by index.

**Request Body** (optional):
```json
{
  "notebook": "my-notebook"
}
```

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "deletedTitle": "Example Article"
  }
}
```

### POST /api/sources/:index/refresh

Refresh/sync a source by index.

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "refreshedTitle": "Example Article"
  }
}
```

---

## Research

### POST /api/research/fast

Trigger fast web research.

**Request Body**:
```json
{
  "query": "latest developments in AI",
  "notebook": "my-notebook",
  "wait": true,
  "timeout_ms": 60000
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | **Yes** | - | Research query |
| `notebook` | string | No | Active | Notebook ID/URL |
| `wait` | boolean | No | `true` | Wait for completion |
| `timeout_ms` | number | No | 60000 | Timeout in ms |

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "status": "completed",
    "results": { ... }
  }
}
```

### POST /api/research/deep

Trigger deep autonomous research (longer, more comprehensive).

**Request Body**:
```json
{
  "query": "comprehensive analysis of renewable energy",
  "notebook": "my-notebook",
  "wait": true,
  "edit_plan": false,
  "timeout_ms": 300000
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | **Yes** | - | Research query |
| `notebook` | string | No | Active | Notebook ID/URL |
| `wait` | boolean | No | `true` | Wait for completion |
| `edit_plan` | boolean | No | `false` | Pause for plan approval |
| `timeout_ms` | number | No | 300000 | Timeout in ms |

### GET /api/research/status

Get current research status.

**Query Parameters**:
| Name | Type | Description |
|------|------|-------------|
| `notebook` | string | Notebook ID/URL |

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "status": "in_progress",
    "progress": "Searching sources..."
  }
}
```

### GET /api/research/results

Get research results.

**Query Parameters**:
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `notebook` | string | Active | Notebook ID/URL |
| `format` | string | `summary` | One of: `summary`, `full`, `sources_only` |

### POST /api/research/import

Import research results as notebook sources.

**Request Body**:
```json
{
  "notebook": "my-notebook"
}
```

**Response**:
```json
{
  "success": true,
  "data": {
    "success": true,
    "importedCount": 5
  }
}
```

### POST /api/research/approve

Approve a pending research plan.

### PUT /api/research/plan

Edit a research plan.

**Request Body**:
```json
{
  "plan": "Updated research plan text",
  "notebook": "my-notebook",
  "approve": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plan` | string | **Yes** | New plan text |
| `notebook` | string | No | Notebook ID/URL |
| `approve` | boolean | No | Also approve after edit |

### GET /api/research/history

Get research history.

**Query Parameters**:
| Name | Type | Description |
|------|------|-------------|
| `notebook` | string | Filter by notebook |
| `all` | boolean | Include all entries |

**Response**:
```json
{
  "success": true,
  "data": {
    "entries": [...],
    "stats": {
      "totalQueries": 25,
      "fastResearchCount": 18,
      "deepResearchCount": 7
    }
  }
}
```

### GET /api/research/cache

Get cache stats or specific entry.

**Query Parameters**:
| Name | Type | Description |
|------|------|-------------|
| `id` | string | Specific entry ID |

### DELETE /api/research/cache

Clear research cache.

**Query Parameters**:
| Name | Type | Description |
|------|------|-------------|
| `notebook` | string | Clear only this notebook's cache |

### GET /api/research/templates

Get research templates.

**Query Parameters**:
| Name | Type | Description |
|------|------|-------------|
| `name` | string | Get specific template |

**Response**:
```json
{
  "success": true,
  "data": {
    "templates": [
      {
        "name": "technical-deep-dive",
        "description": "Deep technical analysis",
        "mode": "deep",
        "tags": ["technical"],
        "queryModifiers": ["comprehensive", "detailed"]
      }
    ]
  }
}
```

### POST /api/research/analyze

Analyze a query to determine optimal research approach.

**Request Body**:
```json
{
  "query": "what are microservices"
}
```

**Response**:
```json
{
  "success": true,
  "data": {
    "recommendedMode": "fast",
    "complexity": "simple",
    "suggestedTemplate": "quick-lookup"
  }
}
```

---

## Sessions

### GET /api/sessions

List active browser sessions.

**Response**:
```json
{
  "success": true,
  "data": {
    "sessions": [
      {
        "id": "session-abc123",
        "notebookUrl": "https://notebooklm.google.com/notebook/...",
        "createdAt": "2025-12-25T02:00:00.000Z",
        "lastUsed": "2025-12-25T02:55:00.000Z"
      }
    ],
    "stats": {
      "active_sessions": 2,
      "max_sessions": 10
    }
  }
}
```

### DELETE /api/sessions/:id

Close a browser session.

**Response**:
```json
{
  "success": true,
  "data": {
    "closed": true,
    "session_id": "session-abc123"
  }
}
```

### POST /api/sessions/:id/reset

Reset a browser session (clears state, keeps open).

---

## System

### GET /api/stats

Get library statistics.

**Response**:
```json
{
  "success": true,
  "data": {
    "total_notebooks": 5,
    "total_queries": 150,
    "active_notebook": "my-notebook",
    "most_used_notebook": "main-research",
    "last_modified": "2025-12-25T02:55:00.000Z"
  }
}
```

### POST /api/cleanup

Cleanup stored data.

**Request Body**:
```json
{
  "confirm": true,
  "preserve_library": true
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `confirm` | boolean | `false` | Actually delete (false = preview) |
| `preserve_library` | boolean | `true` | Keep library.json |

**Response (Preview)**:
```json
{
  "success": true,
  "data": {
    "mode": "preview",
    "totalItems": 15,
    "totalSizeBytes": 1048576,
    "categories": [
      { "name": "debug", "itemCount": 10, "sizeBytes": 524288 },
      { "name": "cache", "itemCount": 5, "sizeBytes": 524288 }
    ]
  }
}
```

### GET /api/queue

Get request queue statistics.

**Response**:
```json
{
  "success": true,
  "data": {
    "pending": 0,
    "processing": false,
    "totalProcessed": 150,
    "totalFailed": 3,
    "averageWaitMs": 250,
    "averageProcessMs": 5000
  }
}
```

### GET /metrics

Prometheus-format metrics.

---

## Usage Examples

### Health Check Before Operations

```bash
# Quick check
curl -s http://127.0.0.1:3456/health/ready | jq '.ready, .checks'

# Deep check (verifies browser auth)
curl -s "http://127.0.0.1:3456/health/ready?deep=true" | jq
```

### Ask a Question

```bash
curl -s -X POST http://127.0.0.1:3456/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Summarize the main points", "notebook": "my-research"}' \
  | jq '.data.answer'
```

### Add Sources in Bulk

```bash
curl -s -X POST http://127.0.0.1:3456/api/sources/bulk \
  -H "Content-Type: application/json" \
  -d '{
    "notebook": "my-research",
    "sources": [
      {"source_type": "website", "content": "https://example.com/article1"},
      {"source_type": "website", "content": "https://example.com/article2"}
    ]
  }' | jq
```

### Fast Research

```bash
curl -s -X POST http://127.0.0.1:3456/api/research/fast \
  -H "Content-Type: application/json" \
  -d '{"query": "latest AI developments", "notebook": "ai-research"}' \
  | jq '.data.results'
```

---

## Error Handling

### Common HTTP Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Bad request (missing/invalid params) |
| 404 | Resource not found |
| 500 | Internal server error |
| 503 | Service not ready |

### Error Response Format

```json
{
  "success": false,
  "error": "Descriptive error message"
}
```

### Timeout Considerations

Browser operations can take 5-60+ seconds. Default timeout is 5 minutes (300000ms).

For long operations like deep research:
```bash
curl --max-time 600 -X POST http://127.0.0.1:3456/api/research/deep \
  -d '{"query": "...", "timeout_ms": 300000}'
```

---

## Rate Limiting

Operations are serialized through a queue. The service processes one browser operation at a time to prevent conflicts. Monitor queue status via `/api/queue` or `/health`.

---

## Authentication Notes

The service uses browser-based authentication via Chrome profile. Authentication is managed outside the API:

1. Run `notebooklm setup-auth` to authenticate
2. Service uses the authenticated Chrome profile
3. Check auth status via `/health/ready?deep=true`

No API keys or tokens are required for API calls.
