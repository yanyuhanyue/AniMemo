"""Explicit, credential-free loopback CONNECT selection; no environment discovery."""
from __future__ import annotations

import hashlib
import http.client
import re
from dataclasses import dataclass

from .http_protocol import HttpFailure, canonical, require


@dataclass(frozen=True)
class EgressSelection:
    endpoint: str | None = None

    def __post_init__(self):
        if self.endpoint is None:
            return
        require(type(self.endpoint) is str and re.fullmatch(
            r'http://(?:127\.0\.0\.1|\[::1\]):[1-9][0-9]{0,4}', self.endpoint), 'EGRESS_INVALID')
        require(1 <= self.port <= 65535, 'EGRESS_INVALID')

    @property
    def host(self):
        return '::1' if self.endpoint.startswith('http://[') else '127.0.0.1'

    @property
    def port(self):
        return int(self.endpoint.rsplit(':', 1)[1])

    def record(self):
        return ({'mode': 'DIRECT'} if self.endpoint is None
                else {'mode': 'HTTP_CONNECT', 'endpoint': self.endpoint})

    @property
    def identity(self):
        return hashlib.sha256(canonical(self.record())).hexdigest()

    @classmethod
    def from_record(cls, value):
        require(type(value) is dict, 'EGRESS_INVALID')
        if value == {'mode': 'DIRECT'}:
            return cls()
        require(set(value) == {'mode', 'endpoint'} and value['mode'] == 'HTTP_CONNECT'
                and type(value['endpoint']) is str, 'EGRESS_INVALID')
        return cls(value['endpoint'])


def select_egress(value=None, *, offline=False):
    require(type(offline) is bool, 'EGRESS_INVALID')
    require(value is None or type(value) is EgressSelection, 'EGRESS_INVALID')
    result = EgressSelection.from_record((value or EgressSelection()).record())
    require(not offline or result.endpoint is None, 'EGRESS_OFFLINE_FORBIDDEN')
    return result


def bound_runtime_egress(endpoint, identity, *, offline=False):
    """Both runtime argv fields must survive the parent's explicit selection."""
    selected = select_egress(EgressSelection(endpoint), offline=offline)
    require((endpoint is None and identity is None)
            or (endpoint is not None and type(identity) is str and identity == selected.identity),
            'EGRESS_BINDING_MISMATCH')
    return selected


class TunnelConnection(http.client.HTTPSConnection):
    """Standard CONNECT/TLS with a bounded, fixed policy check of proxy headers.

    HTTPSConnection.connect uses _tunnel_host as TLS server_hostname, retaining
    target SNI and default certificate/hostname verification. No auth is added.
    """
    tunnel_established = False

    def _tunnel(self):
        try:
            super()._tunnel()
        except http.client.RemoteDisconnected:
            raise HttpFailure('PROXY_CONNECT_EOF') from None
        except TimeoutError:
            raise
        except OSError as error:
            # CPython 3.12's fixed status prefix; never retain the reason phrase.
            matched = re.match(r'^Tunnel connection failed: ([1-5][0-9]{2}) ', str(error))
            status = matched.group(1) if matched is not None else None
            code = 'PROXY_HTTP_' + status if status in {'401', '403', '407', '429'} else 'PROXY_CONNECT_FAILED'
            raise HttpFailure(code) from None
        except http.client.HTTPException:
            raise HttpFailure('PROXY_CONNECT_INVALID') from None
        headers = self.get_proxy_response_headers()
        require(headers is not None and not headers.defects, 'PROXY_CONNECT_INVALID')
        pairs = list(headers.items()) if headers is not None else []
        require(len(pairs) <= 100 and sum(len(k) + len(v) for k, v in pairs) <= 32768,
                'PROXY_CONNECT_INVALID')
        seen = set()
        for name, value in pairs:
            key = name.lower()
            require(re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name)
                and all(c == '\t' or 32 <= ord(c) <= 126 for c in value)
                and key not in seen
                and key not in {'authorization', 'proxy-authorization', 'www-authenticate',
                    'proxy-authenticate', 'set-cookie', 'cookie', 'location', 'transfer-encoding'}
                and (key != 'content-length' or value.strip() == '0'), 'PROXY_CONNECT_INVALID')
            seen.add(key)
        self.tunnel_established = True
