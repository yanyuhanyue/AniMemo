"""Resolve configured managed URLs without requesting a client supplied URL."""

import hashlib
import json
import re
from urllib.parse import unquote, urlsplit

from django.conf import settings

from site_config.models import MediaObject, MediaStorageBackend


def absolute_public_url(value):
    value = str(value or "")
    if value.startswith("/") and not value.startswith("//"):
        origin = str(getattr(settings, "ANIMEMO_MEDIA_PUBLIC_ORIGIN", "") or "").rstrip("/")
        return origin + value if origin else ""
    return value


def url_identity(value):
    """Query/fragment are presentation only; ambiguous paths never identify data.

    Scheme/host and the HTTPS default port are canonicalized. Decode a path
    once, rejecting encoded separators, percent signs, dot segments and control
    characters. The business URL itself is never rewritten by this function.
    """
    value = absolute_public_url(value)
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        port = 443 if parts.port is None else parts.port
        if port == 0:
            return None
        if re.search(r"%(?![0-9a-fA-F]{2})|%(?:2f|5c|25)", parts.path, re.IGNORECASE):
            return None
        path = unquote(parts.path, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        return None
    if "\\" in path or "//" in path or any(ord(char) <= 32 or ord(char) == 127 for char in path):
        return None
    if not path.startswith("/") or any(part in {".", ".."} for part in path.split("/")):
        return None
    return parts.scheme.lower(), parts.hostname.lower(), port, path


def backend_public_bases(backend):
    if backend.backend_type == MediaStorageBackend.BackendType.LOCAL:
        base = str(backend.local_public_base_url or "").rstrip("/")
        if backend.local_root:
            base += "/" + backend.local_root
    else:
        base = str(backend.public_base_url or "").rstrip("/")
    bases = [base]
    # Server supplied recovery configuration, keyed by the backend database ID.
    # This is never derived from Host/X-Forwarded-Host or the poster allowlist.
    history = getattr(settings, "MEDIA_HISTORICAL_PUBLIC_BASES", {})
    if isinstance(history, dict):
        values = history.get(str(backend.pk), [])
        if isinstance(values, (list, tuple)):
            bases.extend(value for value in values if isinstance(value, str))
    return bases


def url_identity_digest(value):
    identity = url_identity(value)
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest() if identity else ""


class _DatabaseCandidates:
    """Live data access for the shared identity-union algorithm."""

    def backends(self):
        return MediaStorageBackend.objects.all().order_by("pk")

    def at_location(self, backend, key):
        return MediaObject.objects.select_related("storage_backend").filter(storage_backend=backend, object_key=key)

    def with_snapshot_digest(self, digest):
        return MediaObject.objects.select_related("storage_backend").filter(public_url_identity=digest)


def resolve_url_candidates(value, *, source=None):
    """Return every possible identity, keeping ambiguity visible to the caller."""
    identity = url_identity(value)
    if identity is None:
        return []
    source = source if source is not None else _DatabaseCandidates()
    matches = {}
    for backend in source.backends():
        for base in backend_public_bases(backend):
            prefix = url_identity(str(base).rstrip("/") + "/")
            if prefix is None or identity[:3] != prefix[:3] or not identity[3].startswith(prefix[3]):
                continue
            key = identity[3][len(prefix[3]):]
            if not key:
                continue
            for media in source.at_location(backend, key):
                matches[media.pk] = media
    # New uploads preserve the server-authorized origin across later base URL
    # configuration changes. A snapshot is identity evidence, never ownership.
    for media in source.with_snapshot_digest(url_identity_digest(value)):
        if url_identity(media.public_url_snapshot) == identity:
            matches[media.pk] = media
    return sorted(matches.values(), key=lambda media: str(media.pk))
