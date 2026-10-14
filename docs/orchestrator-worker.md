# Orchestrator Worker Launch Contract

Work routing authority: [routing-policy.v1.json](../python/omp-work/src/omp_work/routing/routing-policy.v1.json). [Governance inventory](model-policy-governance.md) distinguishes declared routes, session selection and invocation evidence. Worker configuration values below remain examples.

This document defines the execution environment, invocation semantics, sandbox isolation, and model proxy configuration for orchestrator workers executing implementation turns (`implement` stage) and test evaluation (`evaluate` stage).

## Overview

The orchestrator executes untrusted implementation tasks inside an isolated Linux sandbox jail (`worker_sandbox.run_worker`). The worker CLI runs headless, accepts a scoped instruction manifest, communicates with an IPv4 loopback model proxy using a short-lived single-task credential, and accesses the worktree exclusively through scoped `read` and `write` tools.

## Worker Invocation

### Command-line Arguments

Production orchestrator workers are compiled executable binaries dispatched with exact argv:

```json
["/opt/omp/bin/omp", "orchestrator-worker"]
```

The coding-agent CLI host (`packages/coding-agent/src/cli.ts`) detects the `orchestrator-worker` command at startup and enters `runOrchestratorWorker()` directly without initializing interactive TUI components, command registries, session persistence, plugins, MCP servers, hooks, or shell tools.

### Model Proxy URL

`model_proxy_url` is configured on the orchestrator control plane as an IPv4 loopback HTTP endpoint:

```text
http://127.0.0.1:8091
```

The model proxy strictly binds to `127.0.0.1`; binding to any non-loopback interface (such as `0.0.0.0`) is refused at configuration load time.

## Token and Manifest Injection

### Jail Environment

The worker sandbox isolates the process within private user, mount, PID, and network namespaces with a tmpfs root. Only the linked worktree (at `/work`), necessary shared libraries, and designated read-only files are mounted.

The environment passed to the worker contains:

| Variable | Description |
| --- | --- |
| `PATH` | Standard restricted path (`/usr/bin:/bin`) |
| `LANG` / `TERM` / `TZ` | Deterministic terminal environment (`C.UTF-8`, `dumb`, `UTC`) |
| `OMP_TASK_TOKEN` | WorkService capability token for task status and evidence reporting |
| `OMP_WORK_URL` | Loopback URL of the host WorkService (`http://127.0.0.1:<port>`) |
| `OMP_MODEL_PROXY_URL` | Loopback URL of the model proxy (`http://127.0.0.1:<port>`) |
| `OMP_MODEL_TOKEN` | Ephemeral single-task token authorizing upstream model calls |
| `OMP_WORKER_MANIFEST` | Path to the temporary read-only JSON manifest mounted in the jail |

### Worker Manifest Format

The orchestrator generates a temporary JSON manifest file mounted read-only into the sandbox:

```json
{
  "version": 1,
  "instruction": "Implement requested changes according to acceptance criteria.",
  "approach": [
    "Read existing implementation in feature.ts",
    "Apply targeted modifications complying with allowed paths"
  ],
  "allowed_paths": [
    "src/feature.ts",
    "test/feature.test.ts"
  ],
  "candidate_id": "c9a0b12e-1234-5678-90ab-cdef12345678",
  "lease_expires_at": "2026-10-13T01:30:00+00:00",
  "model": {
    "api": "google-generative-ai",
    "id": "gemini-3.8-flash-high",
    "limits": {
      "context_window": 1000000,
      "max_output_tokens": 8192
    },
    "proxy": {
      "baseUrl": "http://127.0.0.1:8091"
    }
  }
}
```

The worker validates that `OMP_MODEL_PROXY_URL` matches `manifest.model.proxy.baseUrl` before initiating any model requests.

## Expiry and Refusal Semantics

1. **Pre-Model Gate**: Before each model invocation, the worker checks the remaining lease duration against `lease_expires_at`. If the deadline has expired, the worker aborts immediately with `deadline exceeded`.
2. **Sticky Failure**: Any failure—including HTTP 4xx/5xx responses from the proxy, tool policy violations, context/token overflows, or unexpected provider stops—sets an immutable failure reason on the worker state. Once tripped, subsequent model calls are rejected immediately, and the process exits non-zero.
3. **Task Revocation**: Upon completion of `run_implement` (whether succeeding, failing, or timing out), the orchestrator control plane executes `revoke_task` against `DELETE /tasks/{task_id}` on the model proxy. Revocation waits for in-flight spend to settle and invalidates the token. Any subsequent attempt to use the revoked `model_token` is refused with HTTP 403 `grant_refused`.

## Scoped Tools

Workers are equipped with exactly two tools rooted at `/work`:

- `read`: Reads file contents up to 1 MB. Reads permit accessing any file inside the `/work` worktree.
- `write`: Writes complete file content within the worktree, restricted to paths matching `allowed_paths`.

Path boundary rules:
- `write` enforces `candidate_git.path_allowed` glob rules against `allowed_paths`:
  - Patterns match repository-relative paths segment by segment.
  - Wildcards (`*`, `?`, `[...]`) cannot cross directory boundaries. Only `**` spans multiple directories.
  - Any attempt to write outside `allowed_paths` raises `ScopedToolRefusal`, marks the worker state as failed, and triggers an immediate agent abort.
- Both `read` and `write` enforce worktree boundaries:
  - Paths must be relative and cannot escape `/work` (e.g. `..` segments or absolute paths are refused).
  - Traversal through `.git` metadata directories or symlinks pointing outside `/work` raises `ScopedToolRefusal` and aborts the agent run.

## Evaluate Stage

Following a successful `implement` stage, the orchestrator triggers `run_evaluate` to run the task's `test_command` within the worker sandbox:
- The command executes in `/work` against the worktree modified by `implement`.
- Exit code 0 signals test success, allowing the candidate to advance to `freeze`.
- Any non-zero exit code or timeout records an evaluation failure, preventing progression without committing changes.

---

## Model Proxy Configuration (`model-proxy.json`)

The model proxy (`python -m omp_work.model_proxy`) runs as an unprivileged host service listening on loopback. It bridges worker calls to upstream LLM providers while enforcing task budgets, lease authorities, and token-level rate limits.

### Configuration File Format

Configuration file `/etc/omp/model-proxy.json`:

```json
{
  "operations": {
    "config_dir": "/etc/omp",
    "state_dir": "/var/lib/omp/state",
    "data_dir": "/var/lib/omp/data"
  },
  "control_capability": "/etc/omp/capabilities/automation.json",
  "host": "127.0.0.1",
  "port": 8091,
  "max_request_bytes": 1048576,
  "max_response_bytes": 4194304,
  "timeout_s": 120.0,
  "transports": {
    "gemini_flash": {
      "api": "google-generative-ai",
      "model": "gemini-3.8-flash-high",
      "origin": "https://generativelanguage.googleapis.com",
      "key_file": "gemini-key",
      "context_tokens": 1000000,
      "max_output_tokens": 8192,
      "input_usd": 0.30,
      "output_usd": 2.50,
      "cache_usd": 0.03,
      "price_version": "p1",
      "thinking_tokens": 500
    },
    "ollama_cloud": {
      "api": "ollama-chat",
      "model": "deepseek-v4.1-flash:cloud",
      "origin": "https://ollama.com",
      "key_file": "ollama-key",
      "context_tokens": 128000,
      "max_output_tokens": 4096,
      "input_usd": 1.00,
      "output_usd": 4.00,
      "cache_usd": 0.50,
      "price_version": "o1",
      "thinking_tokens": 0
    }
  }
}
```

### Security and Key Placement

- **No Secrets in Config**: Secret values are never embedded in `model-proxy.json`. The configuration references key filenames (`key_file: "gemini-key"`) stored under `credentials_dir` (`/etc/omp/credentials/`).
- **File Permissions**:
  - Key files (`/etc/omp/credentials/*`) and capability files (`/etc/omp/capabilities/automation.json`) must be regular files owned by the service user with mode `0600`.
  - Config directory `/etc/omp/credentials` must be mode `0700`.
  - Unsafe permissions cause `ProxyConfig.load()` to fail immediately with exit code 2.
- **Outside Sandbox**: Configuration, capability files, and provider secrets reside entirely on the host and are never mounted into worker jails.

---

## Systemd Service Unit

Below is the service unit definition for running the model proxy under systemd:

```ini
[Unit]
Description=OMP Model Proxy Loopback Relay
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=omp-work
Group=omp-work
UMask=0077
ExecStart=/opt/omp-work/bin/python -m omp_work.model_proxy --config /etc/omp/model-proxy.json
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
KillMode=control-group
LimitNOFILE=65536

# Security Hardening
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=

# Key and state directories
ReadWritePaths=/var/lib/omp/state /var/lib/omp/data
ReadOnlyPaths=/etc/omp

[Install]
WantedBy=multi-user.target
```

### Arch-dev owner user service

The runnable arch-dev assets are `python/omp-work/deploy/model-proxy.json`
and `python/omp-work/deploy/omp-model-proxy.service`. The gateway refreshes
the deployment venv at
`/home/thetu/flood-repos/oh-my-pi-deploy/python/omp-work/.venv`; the user unit
runs its installed `omp_work.model_proxy` with `python -I -B` and requires
`omp-work-postgres.service`.

Install the JSON as `/home/thetu/.config/omp/work-ledger/model-proxy.json`
with mode `0600`, and the unit as
`/home/thetu/.config/systemd/user/omp-model-proxy.service`. As the owner,
reload user units and enable/start `omp-model-proxy.service` with
`systemctl --user`. It listens on `127.0.0.1:8091` and uses the existing
owner service directories:

- Config: `/home/thetu/.config/omp/work-ledger`.
- State: `/home/thetu/.local/state/omp/work-ledger`.
- Data: `/home/thetu/.local/share/omp/work-ledger`.
- Capability: `/home/thetu/.config/omp/work-ledger/capabilities/automation.json`.

The `gemini-key` reference resolves in the existing
`/home/thetu/.config/omp/work-ledger/credentials` directory. Both `gemini-key`
and the database credential `omp_work_app` stay there; never copy credentials
into the deployment tree or worker sandbox. Credential and capability files
must be owner-owned regular files with mode `0600`.

This deployment keeps the example's Gemini route, prices, and limits, with
no Ollama transport. Overflow queues on the primary provider pending owner
approval of an alternate; saturation waits only until the call deadline.
The premium list is `claude-opus-5-5` and `gpt-6-sol`. Its reserve is zero
USD with no authorized models, so both premium models are refused while
ordinary Gemini calls remain eligible under task budgets. These assets do
not record a live service deployment or a settled worker receipt.

### Stop Semantics

- **Graceful Termination**: On `systemctl stop`, systemd sends `SIGTERM`. Uvicorn finishes processing in-flight requests within `TimeoutStopSec=30s` before closing listeners.
- **Spend Accounting**: Revoked and in-flight tasks settle spend durable to PostgreSQL before releasing grant locks.

---

## Fake-Provider Verification Commands

To verify the model proxy against a fake upstream HTTPS provider without external internet access or real provider API keys:

### 1. Generate Local Test Certificates

```bash
mkdir -p /tmp/fake-provider/credentials
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /tmp/fake-provider/key.pem \
  -out /tmp/fake-provider/cert.pem \
  -days 1 -subj "/CN=localhost"
chmod 0600 /tmp/fake-provider/*.pem
```

### 2. Create Isolated Credentials and Control Capability

```bash
echo "dummy-gemini-key" > /tmp/fake-provider/credentials/gemini-key
echo "dummy-ollama-key" > /tmp/fake-provider/credentials/ollama-key
chmod 0600 /tmp/fake-provider/credentials/*

cat <<'EOF' > /tmp/fake-provider/automation.json
{
  "token": "test-control-token-secret",
  "actor_kind": "automation",
  "scopes": ["work.execute"],
  "actor_id": "00000000-0000-0000-0000-000000000001",
  "workspaces": ["00000000-0000-0000-0000-000000000002"]
}
EOF
chmod 0600 /tmp/fake-provider/automation.json
```

### 3. Create Isolated Test Configuration

Write `/tmp/fake-provider/model-proxy.json` configuring loopback endpoints and routing upstream requests to the local fake provider on port 8443:

```bash
cat <<'EOF' > /tmp/fake-provider/model-proxy.json
{
  "operations": {
    "config_dir": "/etc/omp",
    "state_dir": "/var/lib/omp/state",
    "data_dir": "/var/lib/omp/data",
    "credentials_dir": "/tmp/fake-provider/credentials"
  },
  "control_capability": "/tmp/fake-provider/automation.json",
  "host": "127.0.0.1",
  "port": 8091,
  "transports": {
    "gemini_flash": {
      "transport": "google-generative-ai",
      "model": "gemini-3.8-flash-high",
      "upstream_base_url": "https://127.0.0.1:8443",
      "key_file": "gemini-key",
      "max_context_tokens": 1000000,
      "max_output_tokens": 8192,
      "pricing": {
        "version": 1,
        "input_per_million": "0.15",
        "output_per_million": "0.60"
      }
    },
    "ollama_cloud": {
      "transport": "ollama-chat",
      "model": "deepseek-v4.1-flash:cloud",
      "upstream_base_url": "https://127.0.0.1:8443",
      "key_file": "ollama-key",
      "max_context_tokens": 128000,
      "max_output_tokens": 8192,
      "pricing": {
        "version": 1,
        "input_per_million": "0.14",
        "output_per_million": "0.28"
      }
    }
  }
}
EOF
```

### 4. Start Fake Upstream Provider

Run a local Python HTTPS server mimicking Gemini SSE output on port 8443:

```bash
python3 -c '
import http.server, ssl, json

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        data = (b"data: " + json.dumps({
            "candidates": [{"content": {"role": "model", "parts": [{"text": "Verification success"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}
        }).encode() + b"\n\n")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a): pass

server = http.server.HTTPServer(("127.0.0.1", 8443), Handler)
ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
ctx.load_cert_chain("/tmp/fake-provider/cert.pem", "/tmp/fake-provider/key.pem")
server.socket = ctx.wrap_socket(server.socket, server_side=True)
print("Fake provider listening on https://127.0.0.1:8443")
server.serve_forever()
' &
FAKE_PID=$!
```

### 5. Launch Model Proxy with Certificate Trust

Launch the model proxy trusting the fake provider certificate via `SSL_CERT_FILE`:

```bash
SSL_CERT_FILE=/tmp/fake-provider/cert.pem \
  /opt/omp-work/bin/python -m omp_work.model_proxy \
  --config /tmp/fake-provider/model-proxy.json &
PROXY_PID=$!
```

### 6. Verify Launch and Registration

Register a task grant using the control token:

```bash
curl -s -X POST http://127.0.0.1:8091/tasks \
  -H "Authorization: Bearer test-control-token-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "workspace_id": "00000000-0000-0000-0000-000000000002",
    "project_id": "00000000-0000-0000-0000-000000000003",
    "work_id": "00000000-0000-0000-0000-000000000004",
    "job_id": "00000000-0000-0000-0000-000000000005",
    "worker_id": "worker-1",
    "fence": 1,
    "stage": "implement",
    "expires_at": "2026-10-13T02:00:00Z",
    "ceiling_usd": "1.00"
  }'
```

Output:
```json
{
  "id": "<GRANT_ID>",
  "model_token": "<MODEL_TOKEN>",
  "descriptor": {
    "api": "google-generative-ai",
    "id": "gemini-3.8-flash-high",
    "limits": { "context_tokens": 1000000, "max_output_tokens": 8192 },
    "baseUrl": "http://127.0.0.1:8091"
  }
}
```

### 7. Verify Model Invocation

Issue a model call using the returned task `model_token`:

```bash
curl -s -N -X POST "http://127.0.0.1:8091/v1beta/models/gemini-3.8-flash-high:streamGenerateContent?alt=sse" \
  -H "x-goog-api-key: <MODEL_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"Hello"}]}]}'
```

Output:
```text
data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Verification success"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}}
```

### 8. Verify Authority Separation

Ensure task tokens cannot register or revoke tasks:

```bash
# Must return HTTP 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8091/tasks \
  -H "Authorization: Bearer <MODEL_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"workspace_id":"00000000-0000-0000-0000-000000000002"}'

# Must return HTTP 401
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE "http://127.0.0.1:8091/tasks/<GRANT_ID>" \
  -H "Authorization: Bearer <MODEL_TOKEN>"
```

### 9. Cleanup Verification Server and Proxy

```bash
kill $PROXY_PID $FAKE_PID
rm -rf /tmp/fake-provider
```
