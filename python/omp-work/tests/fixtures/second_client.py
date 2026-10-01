#!/usr/bin/env python3
"""OMP-424-s01: stdlib-only second client fixture.

Never imports omp_work.
Routes client_contract operations from contracts/v1/contract.json.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

_DEFAULT_WORKSPACE_ID = "00000000-0000-7000-8000-000000000010"
_DEFAULT_PROJECT_ID = "00000000-0000-7000-8000-000000000020"
_DEFAULT_MISSION_ID = "00000000-0000-7000-8000-000000000030"
_DEFAULT_DECISION_ID = "00000000-0000-7000-8000-000000000041"
_DEFAULT_RECEIPT_ID = "00000000-0000-7000-8000-000000000040"
_DEFAULT_SUBSCRIPTION_ID = "00000000-0000-7000-8000-000000000050"


def find_contract_dir() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent / "src" / "omp_work" / "contracts" / "v1",
        Path.cwd() / "python" / "omp-work" / "src" / "omp_work" / "contracts" / "v1",
        Path.cwd() / "src" / "omp_work" / "contracts" / "v1",
    ]
    for candidate in candidates:
        if (candidate / "contract.json").is_file():
            return candidate
    raise FileNotFoundError("Could not locate omp_work contracts/v1 directory")


def load_contract_data() -> dict[str, Any]:
    contract_dir = find_contract_dir()
    path = contract_dir / "contract.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_contract_sha256() -> str:
    contract_dir = find_contract_dir()
    manifest_path = contract_dir / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    paths = manifest.get("paths", [])
    digest = hashlib.sha256()
    for rel in paths:
        data = (contract_dir / rel).read_bytes()
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def resolve_token(capability: Any) -> str:
    if isinstance(capability, str):
        if os.path.exists(capability):
            try:
                with open(capability, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "token" in data:
                    return str(data["token"])
            except Exception:
                pass
        return capability
    elif isinstance(capability, dict):
        return str(capability.get("token", ""))
    return str(capability or "")


def body_bytes(body: Any) -> bytes:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def signature(key: bytes, idempotency_key: str, body: Any) -> str:
    canonical = body_bytes(body)
    message = idempotency_key.encode("utf-8") + b"\n" + canonical
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def verify_signature(key: bytes, idempotency_key: str, body: Any, header: str | None) -> bool:
    if not isinstance(header, str):
        return False
    try:
        presented = header.encode("ascii")
    except UnicodeEncodeError:
        return False
    expected = signature(key, idempotency_key, body).encode("ascii")
    return hmac.compare_digest(presented, expected)


def make_request(
    method: str,
    url: str,
    headers: dict[str, str],
    json_data: Any | None = None,
) -> None:
    req_headers = dict(headers)
    data = None
    if json_data is not None:
        data = json.dumps(json_data).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    status = 200
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            content = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        content = exc.read()
    except Exception as exc:
        print(json.dumps({"error": str(exc), "status": 500}))
        sys.exit(1)

    try:
        result = json.loads(content.decode("utf-8"))
    except Exception:
        result = {"raw": content.decode("utf-8", errors="replace"), "status": status}

    print(json.dumps(result))
    if status < 200 or status >= 300:
        sys.exit(1)
    sys.exit(0)


def build_instruction(instruction_text: str | None, message_ref: str | None) -> dict[str, Any]:
    text = instruction_text or "Relayed owner intent"
    ref = message_ref or "msg-1"
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "text": text,
        "source_message_ref": ref,
        "owner_authored": True,
        "received_at": now_iso,
    }


def default_intake_payload(
    project_id: str,
    mission_id: str,
    request_file: str | None = None,
) -> dict[str, Any]:
    if request_file and os.path.exists(request_file):
        with open(request_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        here = Path(__file__).resolve().parent
        candidate = here / "second_client_intake.json"
        if candidate.is_file():
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            text = "Deliver the second client proof."
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            data = {
                "mission_id": mission_id,
                "intake": {
                    "archetype": "small_code_change",
                    "source": {"text": text, "sha256": sha, "spans": []},
                    "goal": {"id": "goal-1", "statement": text, "source_span_ids": []},
                },
                "scope": {
                    "project_id": project_id,
                    "risk_policy": "risk-policy",
                    "approval_policy": "approval-policy",
                    "effort_policy": "effort-policy",
                },
            }

    if mission_id:
        data["mission_id"] = str(mission_id)
    if project_id and "scope" in data:
        data["scope"]["project_id"] = str(project_id)
    if "intake" in data and "source" in data["intake"]:
        source_text = data["intake"]["source"].get("text", "")
        data["intake"]["source"]["sha256"] = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="OMP second client")
    parser.add_argument("--config", "-c", help="Path to config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Print {method, path, body} unsent")
    parser.add_argument("--project-id", help="Project ID UUID")
    parser.add_argument("--mission-id", help="Mission ID UUID")
    parser.add_argument("--decision-id", help="Decision ID UUID")
    parser.add_argument("--receipt-id", help="Receipt ID UUID")
    parser.add_argument("--id", help="Generic identifier (receipt_id fallback)")
    parser.add_argument("--revision", type=int, help="Revision integer")
    parser.add_argument("--answer", help="Decision answer string")
    parser.add_argument("--instruction-text", help="Relayed instruction text")
    parser.add_argument("--message-ref", help="Relayed instruction message ref")
    parser.add_argument("--request-file", help="Path to intake request JSON")
    parser.add_argument("--push-key", help="Push key in hex")
    parser.add_argument("--pushes", help="Path to pushes JSON file")
    parser.add_argument("--subscription-id", help="Subscription ID UUID")
    parser.add_argument("--push-url", help="Push URL destination")
    parser.add_argument("--event-types", help="Comma-separated event types")
    parser.add_argument("--detail", action="store_true", help="Request detail=true")

    subparsers = parser.add_subparsers(dest="subcommand")
    subcommands = [
        "operations",
        "identity",
        "projects",
        "context",
        "decisions",
        "status",
        "evidence",
        "events",
        "submit",
        "confirm",
        "answer",
        "pause",
        "resume",
        "cancel",
        "reprioritise",
        "subscribe",
        "inbox",
    ]
    for sub in subcommands:
        sp = subparsers.add_parser(sub)
        sp.add_argument("--config", "-c", default=argparse.SUPPRESS, help="Path to config JSON")
        sp.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="Print {method, path, body} unsent")
        sp.add_argument("--project-id", default=argparse.SUPPRESS, help="Project ID UUID")
        sp.add_argument("--mission-id", default=argparse.SUPPRESS, help="Mission ID UUID")
        sp.add_argument("--decision-id", default=argparse.SUPPRESS, help="Decision ID UUID")
        sp.add_argument("--receipt-id", default=argparse.SUPPRESS, help="Receipt ID UUID")
        sp.add_argument("--id", default=argparse.SUPPRESS, help="Generic identifier")
        sp.add_argument("--revision", type=int, default=argparse.SUPPRESS, help="Revision integer")
        sp.add_argument("--answer", default=argparse.SUPPRESS, help="Decision answer string")
        sp.add_argument("--instruction-text", default=argparse.SUPPRESS, help="Relayed instruction text")
        sp.add_argument("--message-ref", default=argparse.SUPPRESS, help="Relayed instruction message ref")
        sp.add_argument("--request-file", default=argparse.SUPPRESS, help="Path to intake request JSON")
        sp.add_argument("--push-key", default=argparse.SUPPRESS, help="Push key in hex")
        sp.add_argument("--pushes", default=argparse.SUPPRESS, help="Path to pushes JSON file")
        sp.add_argument("--subscription-id", default=argparse.SUPPRESS, help="Subscription ID UUID")
        sp.add_argument("--push-url", default=argparse.SUPPRESS, help="Push URL destination")
        sp.add_argument("--event-types", default=argparse.SUPPRESS, help="Comma-separated event types")
        sp.add_argument("--detail", action="store_true", default=argparse.SUPPRESS, help="Request detail=true")

    args = parser.parse_args()

    # Load contract operations mapping
    contract_data = load_contract_data()
    client_ops = contract_data.get("client_contract", {}).get("operations", [])
    ops_by_name = {op["name"]: op for op in client_ops}

    subcmd = args.subcommand or "operations"

    if subcmd == "operations":
        names = [op["name"] for op in client_ops]
        print(json.dumps({"operations": names, "names": names, "count": len(names)}))
        sys.exit(0)

    if subcmd == "inbox":
        push_key_hex = args.push_key
        pushes_file = args.pushes
        if not push_key_hex or not pushes_file:
            print(json.dumps({"error": "inbox requires --push-key and --pushes"}))
            sys.exit(1)
        try:
            key_bytes = bytes.fromhex(push_key_hex)
        except ValueError:
            print(json.dumps({"error": "invalid hex in --push-key"}))
            sys.exit(1)

        try:
            with open(pushes_file, "r", encoding="utf-8") as f:
                raw_text = f.read().strip()
        except OSError as exc:
            print(json.dumps({"error": f"cannot read pushes file: {exc}"}))
            sys.exit(1)

        pushes: list[dict[str, Any]] = []
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                pushes = parsed
            elif isinstance(parsed, dict):
                if "pushes" in parsed and isinstance(parsed["pushes"], list):
                    pushes = parsed["pushes"]
                else:
                    pushes = [parsed]
        except json.JSONDecodeError:
            for line in raw_text.splitlines():
                line = line.strip()
                if line:
                    try:
                        pushes.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        def get_header(hdrs: dict[str, Any], name: str) -> str | None:
            target = name.lower()
            for k, v in hdrs.items():
                if k.lower() == target:
                    return str(v)
            return None

        for item in pushes:
            headers = item.get("headers", {}) if isinstance(item, dict) else {}
            body = item.get("body") if isinstance(item, dict) else None
            sig_hdr = get_header(headers, "X-OMP-Signature")
            idemp_hdr = get_header(headers, "Idempotency-Key") or ""
            if verify_signature(key_bytes, idemp_hdr, body, sig_hdr):
                accepted.append(item)
            else:
                rejected.append(item)

        status_str = "accepted" if len(rejected) == 0 and len(accepted) > 0 else ("rejected" if len(accepted) == 0 else "mixed")
        print(
            json.dumps(
                {
                    "status": status_str,
                    "accepted": accepted,
                    "rejected": rejected,
                    "verified": accepted,
                    "total": len(pushes),
                    "accepted_count": len(accepted),
                    "rejected_count": len(rejected),
                }
            )
        )
        sys.exit(0)

    # Load config file if provided
    config_data: dict[str, Any] = {}
    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as exc:
            print(json.dumps({"error": f"cannot read config: {exc}"}))
            sys.exit(1)

    base_url = str(config_data.get("base_url") or "http://127.0.0.1:8000").rstrip("/")
    workspace_id = str(args.config and config_data.get("workspace_id") or _DEFAULT_WORKSPACE_ID)
    project_id = str(args.project_id or config_data.get("project_id") or _DEFAULT_PROJECT_ID)
    mission_id = str(args.mission_id or config_data.get("mission_id") or _DEFAULT_MISSION_ID)
    decision_id = str(args.decision_id or config_data.get("decision_id") or _DEFAULT_DECISION_ID)
    receipt_id = str(args.receipt_id or args.id or config_data.get("receipt_id") or _DEFAULT_RECEIPT_ID)
    revision = int(args.revision if args.revision is not None else config_data.get("revision", 1))
    answer = str(args.answer or config_data.get("answer") or "approved")
    instruction = build_instruction(args.instruction_text, args.message_ref)
    token = resolve_token(config_data.get("capability", ""))
    contract_sha = str(config_data.get("contract_sha256") or compute_contract_sha256())

    headers = {
        "Authorization": f"Bearer {token}",
        "X-OMP-Contract-SHA256": contract_sha,
        "X-OMP-Workspace-ID": workspace_id,
    }

    # Route subcommands
    if subcmd == "identity":
        ready_path = "/v1/health/ready"
        if args.dry_run:
            print(json.dumps({"method": "GET", "path": ready_path, "body": None}))
            sys.exit(0)

        # Call ready
        req = urllib.request.Request(f"{base_url}{ready_path}", method="GET")
        try:
            with urllib.request.urlopen(req) as resp:
                if resp.status < 200 or resp.status >= 300:
                    print(json.dumps({"error": "health ready failed", "status": resp.status}))
                    sys.exit(1)
                ready_body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err = json.loads(exc.read().decode("utf-8", errors="replace"))
            print(json.dumps(err))
            sys.exit(1)
        except Exception as exc:
            print(json.dumps({"error": str(exc), "status": 500}))
            sys.exit(1)

        # Also call project.list with auth and contract hash
        list_op = ops_by_name["project.list"]
        list_path = list_op["path"].format(workspace_id=workspace_id)
        req_list = urllib.request.Request(f"{base_url}{list_path}", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req_list) as resp:
                if resp.status < 200 or resp.status >= 300:
                    print(json.dumps({"error": "project.list failed", "status": resp.status}))
                    sys.exit(1)
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode("utf-8", errors="replace"))
            except Exception:
                err = {"status": exc.code, "error": "contract refused or forbidden"}
            print(json.dumps(err))
            sys.exit(1)
        except Exception as exc:
            print(json.dumps({"error": str(exc), "status": 500}))
            sys.exit(1)

        service_fp = ready_body.get("service_fingerprint", "")
        print(json.dumps({"service_fingerprint": service_fp, "contract_sha256": contract_sha}))
        sys.exit(0)

    elif subcmd == "projects":
        op = ops_by_name["project.list"]
        path = op["path"].format(workspace_id=workspace_id)
        if args.detail:
            path += "?detail=true"
        if args.dry_run:
            print(json.dumps({"method": "GET", "path": path, "body": None}))
            sys.exit(0)
        make_request("GET", f"{base_url}{path}", headers)

    elif subcmd == "context":
        op = ops_by_name["project.context"]
        path = op["path"].format(workspace_id=workspace_id, project_id=project_id)
        if args.detail:
            path += "?detail=true"
        if args.dry_run:
            print(json.dumps({"method": "GET", "path": path, "body": None}))
            sys.exit(0)
        make_request("GET", f"{base_url}{path}", headers)

    elif subcmd == "decisions":
        op = ops_by_name["project.decisions"]
        path = op["path"].format(workspace_id=workspace_id, project_id=project_id)
        if args.detail:
            path += "?detail=true"
        if args.dry_run:
            print(json.dumps({"method": "GET", "path": path, "body": None}))
            sys.exit(0)
        make_request("GET", f"{base_url}{path}", headers)

    elif subcmd == "status":
        if args.mission_id:
            op = ops_by_name["mission.status"]
            path = op["path"].format(workspace_id=workspace_id, mission_id=mission_id)
        else:
            op = ops_by_name["project.status"]
            path = op["path"].format(workspace_id=workspace_id, project_id=project_id)
        if args.detail:
            path += "?detail=true"
        if args.dry_run:
            print(json.dumps({"method": "GET", "path": path, "body": None}))
            sys.exit(0)
        make_request("GET", f"{base_url}{path}", headers)

    elif subcmd == "evidence":
        op = ops_by_name["evidence.inspect"]
        path = op["path"].format(workspace_id=workspace_id, receipt_id=receipt_id)
        if args.detail:
            path += "?detail=true"
        if args.dry_run:
            print(json.dumps({"method": "GET", "path": path, "body": None}))
            sys.exit(0)
        make_request("GET", f"{base_url}{path}", headers)

    elif subcmd == "events":
        path = f"/v1/workspaces/{workspace_id}/mission-events"
        if args.dry_run:
            print(json.dumps({"method": "GET", "path": path, "body": None}))
            sys.exit(0)
        make_request("GET", f"{base_url}{path}", headers)

    elif subcmd == "submit":
        op = ops_by_name["mission.intake"]
        path = op["path"].format(workspace_id=workspace_id)
        payload = default_intake_payload(project_id, mission_id, args.request_file)
        if args.dry_run:
            print(json.dumps({"method": "POST", "path": path, "body": payload}))
            sys.exit(0)
        body = {"request_id": str(uuid.uuid4()), "payload": payload}
        make_request("POST", f"{base_url}{path}", headers, body)

    elif subcmd == "confirm":
        op = ops_by_name["mission.scope.confirm"]
        path = op["path"].format(workspace_id=workspace_id, mission_id=mission_id)
        payload = {
            "intent": "confirm_scope",
            "instruction": instruction,
            "mission_id": mission_id,
            "decision_id": decision_id,
            "revision": revision,
        }
        if args.dry_run:
            print(json.dumps({"method": "POST", "path": path, "body": payload}))
            sys.exit(0)
        body = {"request_id": str(uuid.uuid4()), "payload": payload}
        make_request("POST", f"{base_url}{path}", headers, body)

    elif subcmd == "answer":
        op = ops_by_name["decision.answer"]
        path = op["path"].format(workspace_id=workspace_id, decision_id=decision_id)
        payload = {
            "intent": "answer_decision",
            "instruction": instruction,
            "decision_id": decision_id,
            "answer": answer,
        }
        if args.dry_run:
            print(json.dumps({"method": "POST", "path": path, "body": payload}))
            sys.exit(0)
        body = {"request_id": str(uuid.uuid4()), "payload": payload}
        make_request("POST", f"{base_url}{path}", headers, body)

    elif subcmd == "pause":
        op = ops_by_name["mission.pause"]
        path = op["path"].format(workspace_id=workspace_id, mission_id=mission_id)
        payload = {
            "intent": "pause",
            "instruction": instruction,
            "mission_id": mission_id,
        }
        if args.dry_run:
            print(json.dumps({"method": "POST", "path": path, "body": payload}))
            sys.exit(0)
        body = {"request_id": str(uuid.uuid4()), "payload": payload}
        make_request("POST", f"{base_url}{path}", headers, body)

    elif subcmd == "resume":
        op = ops_by_name["mission.resume"]
        path = op["path"].format(workspace_id=workspace_id, mission_id=mission_id)
        payload = {
            "intent": "resume",
            "instruction": instruction,
            "mission_id": mission_id,
        }
        if args.dry_run:
            print(json.dumps({"method": "POST", "path": path, "body": payload}))
            sys.exit(0)
        body = {"request_id": str(uuid.uuid4()), "payload": payload}
        make_request("POST", f"{base_url}{path}", headers, body)

    elif subcmd == "cancel":
        op = ops_by_name["mission.cancel"]
        path = op["path"].format(workspace_id=workspace_id, mission_id=mission_id)
        payload = {
            "intent": "request_cancellation",
            "instruction": instruction,
            "mission_id": mission_id,
        }
        if args.dry_run:
            print(json.dumps({"method": "POST", "path": path, "body": payload}))
            sys.exit(0)
        body = {"request_id": str(uuid.uuid4()), "payload": payload}
        make_request("POST", f"{base_url}{path}", headers, body)

    elif subcmd == "reprioritise":
        op = ops_by_name["mission.reprioritise"]
        path = op["path"].format(workspace_id=workspace_id, mission_id=mission_id)
        payload = {
            "intent": "change_priority",
            "instruction": instruction,
            "mission_id": mission_id,
            "revision": revision,
            "priority": 1,
        }
        if args.dry_run:
            print(json.dumps({"method": "POST", "path": path, "body": payload}))
            sys.exit(0)
        body = {"request_id": str(uuid.uuid4()), "payload": payload}
        make_request("POST", f"{base_url}{path}", headers, body)

    elif subcmd == "subscribe":
        path = "/v1/commands"
        sub_id = args.subscription_id or _DEFAULT_SUBSCRIPTION_ID
        event_types = (
            [t.strip() for t in args.event_types.split(",") if t.strip()]
            if args.event_types
            else ["mission.started", "mission.completed"]
        )
        sub_payload: dict[str, Any] = {
            "subscription_id": sub_id,
            "event_types": event_types,
        }
        if args.push_url:
            sub_payload["push_url"] = args.push_url

        envelope = {
            "api_version": "work.omp.dev/v1",
            "workspace_id": workspace_id,
            "operation_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "command": {
                "type": "put_event_subscription",
                "payload": sub_payload,
            },
        }
        if args.dry_run:
            print(json.dumps({"method": "POST", "path": path, "body": envelope}))
            sys.exit(0)
        make_request("POST", f"{base_url}{path}", headers, envelope)

    else:
        print(json.dumps({"error": f"unknown subcommand: {subcmd}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
