#!/usr/bin/env python3
"""Complete AniMemo first-run setup against an isolated CI loopback stack."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlsplit


_ISOLATED_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.example\.test$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_isolated_target(base_url, host, *, confirm_isolated):
    if not confirm_isolated:
        raise ValueError("--confirm-isolated is required")
    parsed = urlsplit(str(base_url).strip())
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("first-run CI smoke only accepts a plain HTTP loopback base URL")
    normalized_host = str(host).strip().lower()
    if not _ISOLATED_HOST.fullmatch(normalized_host):
        raise ValueError("first-run CI smoke Host must be an isolated *.example.test name")
    return parsed.geturl().rstrip("/"), normalized_host


def _json_request(opener, request, *, timeout, expected_status):
    with opener.open(request, timeout=timeout) as response:
        status = int(response.status)
        payload = json.loads(response.read().decode("utf-8"))
    if status != expected_status:
        raise RuntimeError(f"unexpected first-run API status: {status}")
    if not isinstance(payload, dict):
        raise RuntimeError("first-run API returned a non-object JSON response")
    return payload


def complete_setup(*, opener, base_url, host, code, username, email, password, timeout=20):
    common_headers = {"Accept": "application/json", "Host": host}
    csrf_payload = _json_request(
        opener,
        urllib.request.Request(f"{base_url}/api/v1/auth/csrf/", headers=common_headers),
        timeout=timeout,
        expected_status=200,
    )
    csrf_token = csrf_payload.get("csrf_token")
    if not isinstance(csrf_token, str) or not csrf_token:
        raise RuntimeError("first-run CSRF endpoint did not return a token")

    setup_payload = json.dumps(
        {
            "code": code,
            "username": username,
            "email": email,
            "password": password,
            "password_confirm": password,
        }
    ).encode("utf-8")
    setup_request = urllib.request.Request(
        f"{base_url}/api/v1/setup/",
        data=setup_payload,
        headers={
            **common_headers,
            "Content-Type": "application/json",
            "X-CSRFToken": csrf_token,
        },
        method="POST",
    )
    completed = _json_request(
        opener,
        setup_request,
        timeout=timeout,
        expected_status=201,
    )
    if completed.get("state") != "initialized":
        raise RuntimeError("first-run setup did not report initialized state")

    status_payload = _json_request(
        opener,
        urllib.request.Request(
            f"{base_url}/api/v1/setup/status/",
            headers=common_headers,
        ),
        timeout=timeout,
        expected_status=200,
    )
    if status_payload.get("state") != "initialized" or status_payload.get("accepting_setup") is not False:
        raise RuntimeError("first-run status remained open after setup completion")
    return status_payload


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--code-stdin", action="store_true")
    parser.add_argument("--confirm-isolated", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        base_url, host = validate_isolated_target(
            args.base_url,
            args.host,
            confirm_isolated=args.confirm_isolated,
        )
        if not args.code_stdin:
            raise ValueError("--code-stdin is required so the setup code is never placed in argv")
        password = os.environ.get(args.password_env, "")
        if not password:
            raise ValueError(f"password environment variable is empty: {args.password_env}")
        code = sys.stdin.read(4097).strip()
        if not code or len(code) > 4096:
            raise ValueError("setup code stdin is empty or unexpectedly large")
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        complete_setup(
            opener=opener,
            base_url=base_url,
            host=host,
            code=code,
            username=args.username,
            email=args.email,
            password=password,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"First-run setup smoke failed: {error}", file=sys.stderr)
        return 1
    print("First-run setup smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
