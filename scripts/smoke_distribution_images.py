#!/usr/bin/env python3
"""Read runtime resources from the actual locally built API and Web images."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
from urllib.parse import urlsplit

API_PROBE = r'''
import importlib.metadata
import json
from pathlib import Path
import ssl
import sys
from zoneinfo import ZoneInfo

def offline(event, arguments):
    if event.startswith(("socket.", "subprocess.")):
        raise RuntimeError("IMAGE_RESOURCE_SMOKE_NETWORK_OR_PROCESS_FORBIDDEN")
sys.addaudithook(offline)

import certifi
import django
import psycopg
django.setup()
from django.contrib.auth.password_validation import CommonPasswordValidator
from django.db.migrations.loader import MigrationLoader
from django.template.loader import get_template

for name in ("admin/base.html", "rest_framework/api.html", "drf_spectacular/swagger_ui_split.html"):
    get_template(name)
passwords = CommonPasswordValidator().passwords
assert "password" in passwords and len(passwords) > 1000
assert importlib.metadata.version("psycopg") == psycopg.__version__
assert importlib.metadata.version("psycopg-binary") == psycopg.__version__
ssl.create_default_context(cafile=certifi.where())
ZoneInfo("Asia/Shanghai")
migrations = MigrationLoader(None, ignore_no_migrations=True).disk_migrations
assert {"journal", "plugin_host", "site_config", "integrations"} <= {app for app, name in migrations}
root = Path("/app/backend/staticfiles")
manifest = json.loads((root / "staticfiles.json").read_text(encoding="utf-8"))
assert manifest["paths"]
for relative in manifest["paths"].values():
    resource = (root / relative).resolve(strict=True)
    assert resource.is_relative_to(root) and resource.is_file()
print(json.dumps({"templates": 3, "password_dictionary": "PASS", "certificates": "PASS", "distribution_metadata": "PASS", "migrations": len(migrations), "static_resources": len(manifest["paths"])}))
'''


class _PageResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag, attrs):
        attribute = {"script": "src", "link": "href", "img": "src"}.get(tag)
        value = dict(attrs).get(attribute) if attribute else None
        if not value:
            return
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            return
        path = PurePosixPath(parsed.path)
        if not parsed.path or ".." in path.parts or "\\" in parsed.path:
            raise ValueError("Web resource path escapes the image root")
        self.paths.add("/usr/share/nginx/html/" + parsed.path.lstrip("/"))


def web_resources(html: str) -> list[str]:
    parser = _PageResources()
    parser.feed(html)
    if not any(path.endswith(".js") for path in parser.paths):
        raise ValueError("Web image has no JavaScript entrypoint")
    return sorted(parser.paths)


def _docker(*arguments: str, source: str | None = None) -> str:
    environment = {
        name: value for name, value in os.environ.items()
        if name.upper() in {"PATH", "HOME", "SYSTEMROOT", "WINDIR"}
    }
    completed = subprocess.run(
        ["docker", "--host", "unix:///var/run/docker.sock", *arguments],
        input=source, text=True, encoding="utf-8", capture_output=True,
        env=environment, check=True, timeout=120,
    )
    return completed.stdout.strip()


def _image_identity(image: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9./:@_-]*", image):
        raise ValueError("Image must be a fixed local image name or digest")
    identity = _docker("image", "inspect", "--format", "{{.Id}}", image)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
        raise ValueError("Local image identity is invalid")
    return identity


def smoke_images(api_image: str, web_image: str) -> dict[str, object]:
    api = _image_identity(api_image)
    web = _image_identity(web_image)
    isolated = (
        "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    )
    api_result = json.loads(_docker(
        *isolated, "--interactive", "--workdir", "/app/backend",
        "--env", "ANIMEMO_BUILD_STATIC=1", "--env", "DJANGO_SETTINGS_MODULE=config.settings",
        "--env", "PYTHONPATH=/app:/app/backend", "--env", "PYTHON_DOTENV_DISABLED=1",
        "--entrypoint", "python", api, "-P", "-B", "-", source=API_PROBE,
    ))
    html = _docker(*isolated, "--entrypoint", "/bin/cat", web, "/usr/share/nginx/html/index.html")
    resources = [
        *web_resources(html),
        "/etc/nginx/animemo/default.conf.template",
        "/usr/local/bin/animemo-nginx-entrypoint",
        "/usr/local/libexec/animemo/resolve-edge-gateway.awk",
    ]
    _docker(
        *isolated, "--entrypoint", "/bin/sh", web, "-eu", "-c",
        'for resource in "$@"; do test -s "$resource"; done', "image-resource-smoke", *resources,
    )
    return {"status": "PASS", "api_image": api, "web_image": web, "api": api_result, "web_resources": len(resources)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-image", required=True)
    parser.add_argument("--web-image", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = smoke_images(arguments.api_image, arguments.web_image)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            parser.exit(1, f"Distribution image resource smoke failed: {error.stderr.strip()}\n")
        parser.exit(1, f"Distribution image resource smoke failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
