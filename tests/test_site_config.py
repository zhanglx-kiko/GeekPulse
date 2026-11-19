import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from site_config import SiteConfig


class SiteConfigTests(unittest.TestCase):
    def test_canonical_public_origin_and_default_loopback_port(self):
        site = SiteConfig.from_env({'GEEKPULSE_PUBLIC_ORIGIN': 'https://Gateway.Example.COM:443/'})
        self.assertEqual((site.origin, site.authority, site.sub2api_port),
                         ('https://gateway.example.com', 'gateway.example.com', 8080))

    def test_missing_or_invalid_origin_fails_closed(self):
        for value in ('', 'http://gateway.example.com', 'https://gateway.example.com/login',
                      'https://gateway.example.com/?', 'https://gateway.example.com/#',
                      'https://user@gateway.example.com', 'https://gateway.example.com:',
                      'https://127.0.0.1', 'https://[::1]', 'https://localhost', 'https://lab.internal'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'GEEKPULSE_PUBLIC_ORIGIN'):
                SiteConfig.from_env({'GEEKPULSE_PUBLIC_ORIGIN': value})

    def test_upstream_port_must_be_valid(self):
        for port in ('', '0', '65536', 'host:8080', ' 8080', '12.5'):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, 'GEEKPULSE_SUB2API_PORT'):
                SiteConfig.from_env({'GEEKPULSE_PUBLIC_ORIGIN': 'https://gateway.example.com', 'GEEKPULSE_SUB2API_PORT': port})

    def test_brand_cannot_point_off_site_or_outside_an_asset_path(self):
        for key, value in [('GEEKPULSE_LOGO_PATH', 'https://assets.example.com/icon.svg'),
                           ('GEEKPULSE_LOGO_PATH', '//other.example/logo.svg'), ('GEEKPULSE_LOGO_PATH', '/../icon.png'),
                           ('GEEKPULSE_RECEIPT_NAMESPACE', 'bad/key'), ('GEEKPULSE_SITE_NAME', 'line\nbreak')]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                SiteConfig.from_env({'GEEKPULSE_PUBLIC_ORIGIN': 'https://gateway.example.com', key: value})
