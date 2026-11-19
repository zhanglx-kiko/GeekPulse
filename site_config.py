"""Deployment-owned origin and the local port of the trusted Sub2API service."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from runner import ProbeError, normalize_endpoint


@dataclass(frozen=True)
class SiteConfig:
    authority: str
    sub2api_port: int
    site_name: str = 'GeekPulse'
    logo_path: str = '/model-lab/pulse.svg'
    receipt_namespace: str = 'geekpulse-lab'

    @property
    def origin(self):
        return 'https://' + self.authority

    @classmethod
    def from_env(cls, env):
        origin = env.get('GEEKPULSE_PUBLIC_ORIGIN', '')
        try:
            endpoint = normalize_endpoint(origin, 'responses')
            parsed = urlsplit(origin)
            if (parsed.path not in ('', '/') or '?' in origin or '#' in origin or
                    parsed.netloc.endswith(':') or ':' in endpoint.host or
                    endpoint.host.split('.')[-1].isdigit()):
                raise ValueError
        except (ValueError, ProbeError):
            raise ValueError('Set GEEKPULSE_PUBLIC_ORIGIN to your public HTTPS DNS origin (no path, credentials or query).') from None
        port = env.get('GEEKPULSE_SUB2API_PORT', '8080')
        if not port.isascii() or not port.isdecimal() or len(port) > 5 or not 1 <= int(port) <= 65535:
            raise ValueError('GEEKPULSE_SUB2API_PORT must be a port between 1 and 65535.')
        authority = endpoint.host if endpoint.port == 443 else f'{endpoint.host}:{endpoint.port}'
        name = env.get('GEEKPULSE_SITE_NAME', 'GeekPulse')
        logo = env.get('GEEKPULSE_LOGO_PATH', '/model-lab/pulse.svg')
        namespace = env.get('GEEKPULSE_RECEIPT_NAMESPACE', 'geekpulse-lab')
        if not name.strip() or len(name) > 64 or not name.isprintable():
            raise ValueError('GEEKPULSE_SITE_NAME must be a short, printable site name.')
        if not re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:svg|png|ico|webp)', logo):
            raise ValueError('GEEKPULSE_LOGO_PATH must be a same-origin image path.')
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', namespace):
            raise ValueError('GEEKPULSE_RECEIPT_NAMESPACE must be a lowercase storage prefix.')
        return cls(authority, int(port), name, logo, namespace)
