#!/usr/bin/env python3
"""Prove the automation GitHub token can push but cannot administer.

The probe is a read-with-a-ceiling: it refuses to send any write once a read
shows the token carries admin permission or reaches branch protection, and it
stops sending writes as soon as one unexpectedly succeeds. Every write it does
send is a denial probe or a description no-op expected to be refused. The token
is read from GH_TOKEN and never printed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = "2022-11-28"
USER_AGENT = "omp-automation-github-probe"
TIMEOUT_SECONDS = 15
# A token that cannot administer is refused on the write endpoints with 403
# (insufficient permission) or 404 (feature/scope invisible to it).
WRITE_FAILURE_STATUSES = frozenset({403, 404})


class Refused(Exception):
    """The probe refuses to run (bad configuration) before any request."""


def loopback_host(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_api_url(api_url: str) -> str:
    split = urllib.parse.urlsplit(api_url)
    if not split.hostname:
        raise Refused("api-url has no host")
    if split.scheme == "https":
        return api_url.rstrip("/")
    if split.scheme == "http" and loopback_host(split.hostname):
        return api_url.rstrip("/")
    raise Refused("api-url must be https (or http on loopback)")


def validate_repo(repo: str) -> str:
    parts = [part for part in repo.strip().split("/") if part]
    if len(parts) != 2:
        raise Refused(f"repo must be OWNER/NAME, got {repo!r}")
    return f"{parts[0]}/{parts[1]}"


class Probe:
    def __init__(self, *, api_url: str, token: str, repo: str, branch: str, org: str | None) -> None:
        self.api_url = api_url
        self.token = token
        self.repo = repo
        self.branch = branch
        self.org = org
        self.checks: dict[str, dict[str, object]] = {}

    def record(self, name: str, ok: bool, detail: str) -> bool:
        self.checks[name] = {"ok": ok, "detail": detail}
        return ok

    def send(self, method: str, path: str, body: object | None = None) -> tuple[int, bytes]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.api_url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", API_VERSION)
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    @staticmethod
    def decode(raw: bytes) -> dict:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def check_can_push(self) -> dict | None:
        path = f"/repos/{self.repo}"
        try:
            status, raw = self.send("GET", path)
        except OSError as error:
            self.record("can_push", False, f"GET {path} failed: {error}")
            return None
        if status != 200:
            self.record("can_push", False, f"GET {path} -> {status}")
            return None
        body = self.decode(raw)
        permissions = body.get("permissions")
        if not isinstance(permissions, dict):
            self.record("can_push", False, f"GET {path} -> 200 without permissions")
            return None
        push = permissions.get("push") is True
        admin = permissions.get("admin") is True
        detail = f"GET {path} -> 200 permissions.push={str(push).lower()} permissions.admin={str(admin).lower()}"
        if admin:
            self.record("can_push", False, f"{detail}; token carries admin, refusing every write")
            return None
        if not push:
            self.record("can_push", False, f"{detail}; token cannot push")
            return None
        self.record("can_push", True, detail)
        return body

    def check_branch_protection_denied(self) -> bool:
        read_path = f"/repos/{self.repo}/branches/{self.branch}/protection"
        try:
            read_status, _ = self.send("GET", read_path)
        except OSError as error:
            self.record("branch_protection_denied", False, f"GET {read_path} failed: {error}")
            return False
        if read_status not in WRITE_FAILURE_STATUSES:
            self.record(
                "branch_protection_denied",
                False,
                f"GET {read_path} -> {read_status} (expected 403/404); refusing every write",
            )
            return False
        write_path = f"{read_path}/enforce_admins"
        try:
            write_status, _ = self.send("POST", write_path)
        except OSError as error:
            self.record("branch_protection_denied", False, f"POST {write_path} failed: {error}")
            return False
        if write_status not in WRITE_FAILURE_STATUSES:
            self.record(
                "branch_protection_denied",
                False,
                f"POST {write_path} -> {write_status} (expected 403/404)",
            )
            return False
        self.record(
            "branch_protection_denied",
            True,
            f"GET {read_path} -> {read_status}; POST {write_path} -> {write_status}",
        )
        return True

    def check_repo_settings_denied(self, repo_body: dict) -> bool:
        path = f"/repos/{self.repo}"
        body = {"description": repo_body.get("description")}
        try:
            status, _ = self.send("PATCH", path, body)
        except OSError as error:
            self.record("repo_settings_denied", False, f"PATCH {path} failed: {error}")
            return False
        if status not in WRITE_FAILURE_STATUSES:
            self.record(
                "repo_settings_denied",
                False,
                f"PATCH {path} -> {status} with the current description (expected 403/404)",
            )
            return False
        self.record("repo_settings_denied", True, f"PATCH {path} with the current description -> {status}")
        return True

    def check_org_settings_denied(self) -> bool:
        if not self.org:
            self.record("org_settings_denied", True, "skipped")
            return True
        path = f"/orgs/{self.org}"
        try:
            read_status, raw = self.send("GET", path)
        except OSError as error:
            self.record("org_settings_denied", False, f"GET {path} failed: {error}")
            return False
        if read_status != 200:
            self.record("org_settings_denied", False, f"GET {path} -> {read_status}")
            return False
        body = {"description": self.decode(raw).get("description")}
        try:
            write_status, _ = self.send("PATCH", path, body)
        except OSError as error:
            self.record("org_settings_denied", False, f"PATCH {path} failed: {error}")
            return False
        if write_status not in WRITE_FAILURE_STATUSES:
            self.record(
                "org_settings_denied",
                False,
                f"PATCH {path} -> {write_status} with the current description (expected 403/404)",
            )
            return False
        self.record("org_settings_denied", True, f"PATCH {path} with the current description -> {write_status}")
        return True

    def run(self) -> dict[str, dict[str, object]]:
        repo_body = self.check_can_push()
        if repo_body is None:
            return self.checks
        if not self.check_branch_protection_denied():
            return self.checks
        if not self.check_repo_settings_denied(repo_body):
            return self.checks
        self.check_org_settings_denied()
        return self.checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe that the automation GitHub token can push but not administer.")
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--org")
    parser.add_argument("--api-url", default="https://api.github.com")
    args = parser.parse_args(argv)

    try:
        api_url = validate_api_url(args.api_url)
        repo = validate_repo(args.repo)
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            raise Refused("GH_TOKEN is required")
    except Refused as error:
        print(json.dumps({"error": str(error)}))
        print(f"github-probe: refused: {error}", file=sys.stderr)
        return 2

    probe = Probe(api_url=api_url, token=token, repo=repo, branch=args.branch, org=args.org)
    checks = probe.run()
    print(json.dumps(checks))
    return 0 if checks and all(check["ok"] for check in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
