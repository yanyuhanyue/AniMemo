"""Fixed anonymous HTTP worker. It writes untrusted bytes and never authorizes use."""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import ssl
import sys
import time
from pathlib import Path

from .http_protocol import (
    CONTROL_BYTES,
    PROGRESS_BYTES,
    PROGRESS_FRAMES,
    PROGRESS_STEP,
    PROTOCOL,
    HttpFailure,
    HttpSelection,
    asset_redirect,
    canonical,
    require,
    strict_json,
)
from .safe_files import exclusive_file
from .egress import EgressSelection, TunnelConnection, select_egress

_UNIQUE = frozenset({'content-length', 'content-type', 'content-encoding', 'transfer-encoding',
    'location', 'set-cookie', 'authorization', 'proxy-authorization', 'link', 'retry-after',
    'www-authenticate', 'proxy-authenticate', 'deprecation', 'sunset'})
_RETURN_HEADERS = frozenset({'content-length', 'content-type', 'content-encoding',
    'transfer-encoding', 'link', 'deprecation', 'sunset'})


def response_headers(pairs):
    require(type(pairs) is list and len(pairs) <= 100, 'HEADERS_INVALID')
    result, total = {}, 0
    for name, value in pairs:
        require(type(name) is str and type(value) is str, 'HEADERS_INVALID')
        total += len(name) + len(value)
        require(total <= 32768 and re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name)
            and all(c == '\t' or 32 <= ord(c) < 127 or 128 <= ord(c) <= 255 for c in value), 'HEADERS_INVALID')
        name = name.lower()
        require(name not in result or name not in _UNIQUE, 'HEADERS_INVALID')
        result[name] = value.strip()
    require(not set(result) & {'set-cookie', 'authorization', 'proxy-authorization'}, 'HEADERS_INVALID')
    length, transfer = result.get('content-length'), result.get('transfer-encoding')
    require(length is None or len(length) <= 20 and re.fullmatch(r'0|[1-9][0-9]*', length), 'LENGTH_INVALID')
    require(result.get('content-encoding', 'identity').lower() == 'identity'
        and (transfer is None or transfer.lower() == 'chunked' and length is None), 'ENCODING_INVALID')
    return result


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    require(remaining > 0, 'DEADLINE')
    return min(30, remaining)


def execute(selection, deadline, *, progress=None, egress=None):
    """Only called after GO; parent can terminate DNS, connect, TLS and read stalls."""
    host, path, accept = selection.target()
    egress = select_egress(egress)
    observations = []
    context = ssl.create_default_context()
    require(context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED,
            'TLS_POLICY_INVALID')
    def observe(phase, attempt, status, size):
        if progress is not None:
            progress(phase, attempt, status, size)
    for attempt in range(2):
        connection, response = None, None
        size, observed_status = 0, None
        primary = None
        try:
            observe('REQUEST', attempt, None, 0)
            if egress.endpoint is None:
                connection = http.client.HTTPSConnection(host, timeout=_remaining(deadline), context=context)
            else:
                connection = TunnelConnection(egress.host, egress.port,
                    timeout=_remaining(deadline), context=context)
                connection.set_tunnel(host, 443)
            headers = {'Accept': accept, 'Accept-Encoding': 'identity',
                'User-Agent': 'AniMemo-Anonymous-Bootstrap/1', 'Connection': 'close'}
            if host == 'api.github.com':
                headers['X-GitHub-Api-Version'] = '2022-11-28'
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            require(type(response.status) is int and 100 <= response.status <= 599, 'STATUS_INVALID')
            observed_status = response.status
            safe_observation = {'kind': selection.kind, 'host': host, 'path': path.split('?', 1)[0],
                'status': response.status, 'authorization': False, 'cookie': False}
            if egress.endpoint is not None:
                require(connection.tunnel_established is True, 'EGRESS_BINDING_MISMATCH')
                safe_observation['egress'] = {**egress.record(), 'target': host, 'connect': 'ESTABLISHED'}
            observations.append(safe_observation)
            observe('HEADERS', attempt, response.status, 0)
            parsed = response_headers(response.getheaders())
            if response.status == 302 and selection.kind == 'GITHUB_ASSET' and attempt == 0:
                host, path = asset_redirect(parsed.get('location'))
                continue
            require(response.status == 200, 'HTTP_' + str(response.status))
            require('location' not in parsed, 'HEADERS_INVALID')
            length = parsed.get('content-length')
            require(length is None or int(length) <= selection.maximum_bytes, 'TOO_LARGE')
            content_type = parsed.get('content-type', '').split(';', 1)[0].strip().lower()
            allowed = {'application/json', 'application/vnd.github+json'}
            if selection.kind == 'GITHUB_ASSET':
                allowed = {'application/octet-stream', 'application/x-tar', 'application/zip'}
            elif selection.kind.startswith('TUF_'):
                allowed |= {'text/plain', 'application/octet-stream'}
            elif selection.kind == 'ACTIONS_BUNDLE':
                allowed |= {'application/x-snappy'}
            require(content_type in allowed, 'CONTENT_TYPE_INVALID')
            digest, size, reported = hashlib.sha256(), 0, None
            with exclusive_file(Path.cwd() / 'body.bin') as output:
                while True:
                    _remaining(deadline)
                    if connection.sock is not None:
                        connection.sock.settimeout(_remaining(deadline))
                    chunk = response.read1(min(65536, selection.maximum_bytes + 1 - size))
                    if not chunk:
                        _remaining(deadline)
                        break
                    size += len(chunk)
                    require(size <= selection.maximum_bytes, 'TOO_LARGE')
                    if reported is None or size // PROGRESS_STEP > reported // PROGRESS_STEP:
                        observe('BODY', attempt, response.status, size)
                        reported = size
                    _remaining(deadline)
                    output.write(chunk)
                    digest.update(chunk)
                require(length is None or size == int(length), 'TRUNCATED')
                if selection.kind == 'GITHUB_ASSET':
                    require(size == selection.fields['size'] and digest.hexdigest() == selection.fields['sha256'], 'ASSET_BYTES_MISMATCH')
                output.flush()
                os.fsync(output.fileno())
            observe('COMPLETE', attempt, response.status, size)
            return {'result': 'BYTES_WRITTEN', 'status': 200, 'size': size, 'sha256': digest.hexdigest(),
                'headers': {key: value for key, value in parsed.items() if key in _RETURN_HEADERS},
                'observations': observations}
        except BaseException as error:
            primary = error
            error.observations = tuple(observations)
            try:
                observe('FAILED', attempt, observed_status, min(size, selection.maximum_bytes))
            except (HttpFailure, OSError):
                pass  # Diagnostic failure cannot replace the original failure.
            raise
        finally:
            failed = False
            for resource in (response, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except OSError:
                        failed = True
            if failed and primary is None:
                raise HttpFailure('NETWORK_CLOSE_FAILED')
    raise HttpFailure('REDIRECT_INVALID')


def main():
    # Only a nonce and optional expected configuration digest are accepted.
    if len(sys.argv) not in (2, 4) or re.fullmatch(r'[0-9a-f]{32}', sys.argv[1]) is None:
        return 2
    nonce = sys.argv[1]
    expected_egress = None
    if len(sys.argv) == 4:
        if sys.argv[2] != '--egress-sha256' or re.fullmatch(r'[0-9a-f]{64}', sys.argv[3]) is None:
            return 2
        expected_egress = sys.argv[3]
    ready = {'protocol': PROTOCOL, 'event': 'READY', 'nonce': nonce}
    if expected_egress is not None:
        ready['egress_identity'] = expected_egress
    sys.stdout.buffer.write(canonical(ready))
    sys.stdout.buffer.flush()
    identity = None
    progress_size, progress_count = 0, 0
    try:
        raw = sys.stdin.buffer.readline(CONTROL_BYTES + 1)
        require(0 < len(raw) <= CONTROL_BYTES and raw.endswith(b'\n') and sys.stdin.buffer.read(1) == b'')
        go = strict_json(raw)
        fields = {'protocol', 'event', 'nonce', 'deadline', 'selection'}
        if expected_egress is not None:
            fields.add('egress')
        require(type(go) is dict and set(go) == fields
            and go['protocol'] == PROTOCOL and go['event'] == 'GO' and go['nonce'] == nonce)
        egress = EgressSelection.from_record(go['egress']) if expected_egress is not None else EgressSelection()
        require(expected_egress is None or egress.endpoint is not None and egress.identity == expected_egress,
                'EGRESS_BINDING_MISMATCH')
        require(type(go['deadline']) in (int, float) and 0 < go['deadline'] - time.monotonic() <= 900)
        selection = HttpSelection.from_record(go['selection'])
        identity = selection.identity
        def progress(phase, request_index, status, size):
            nonlocal progress_size, progress_count
            progress_count += 1
            encoded = canonical({'protocol': PROTOCOL, 'event': 'PROGRESS', 'nonce': nonce,
                'identity': identity, 'sequence': progress_count, 'phase': phase,
                'request_index': request_index, 'http_status': status,
                'received_bytes': size, 'observed_monotonic': time.monotonic()})
            progress_size += len(encoded)
            require(progress_count <= PROGRESS_FRAMES and progress_size <= PROGRESS_BYTES,
                    'PROTOCOL_INVALID')
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()
        if egress.endpoint is None:
            result = execute(selection, go['deadline'], progress=progress)
        else:
            result = execute(selection, go['deadline'], progress=progress, egress=egress)
        code = 0
    except HttpFailure as error:
        result, code = {'result': 'FAILED', 'code': error.code.removeprefix('BOOTSTRAP_HTTP_'),
            'observations': list(error.observations)}, 2
    except TimeoutError as error:
        result, code = {'result': 'FAILED', 'code': 'DEADLINE', 'observations': list(getattr(error, 'observations', ()))}, 2
    except ssl.SSLCertVerificationError as error:
        result, code = {'result': 'FAILED', 'code': 'TLS_IDENTITY_FAILED', 'observations': list(getattr(error, 'observations', ()))}, 2
    except ssl.SSLError as error:
        result, code = {'result': 'FAILED', 'code': 'TLS_HANDSHAKE_FAILED', 'observations': list(getattr(error, 'observations', ()))}, 2
    except (OSError, http.client.HTTPException) as error:
        result, code = {'result': 'FAILED', 'code': 'NETWORK_FAILED', 'observations': list(getattr(error, 'observations', ()))}, 2
    except Exception as error:  # noqa: BLE001 - never print arbitrary worker exception text.
        result, code = {'result': 'FAILED', 'code': 'WORKER_FAILED', 'observations': list(getattr(error, 'observations', ()))}, 2
    encoded = canonical(dict(protocol=PROTOCOL, event='RESULT', nonce=nonce, identity=identity, **result))
    if len(encoded) > CONTROL_BYTES - 256 - progress_size:
        return 2
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
