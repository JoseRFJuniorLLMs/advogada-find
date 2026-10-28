"""Offline public-suffix identities and HTTP origin validation."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None,
                               include_psl_private_domains=True)


def company_domain(host: str) -> str:
    host = host.lower().strip('.').encode('idna').decode('ascii')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('IP addresses are not company domains')
    parts = _extract(host)
    if not parts.domain or not parts.suffix:
        raise ValueError('Domain must have a public suffix')
    return parts.top_domain_under_public_suffix


def valid_url(url: str, domain: str | None = None) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            return False
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return False
        identity = company_domain(parsed.hostname)
        return domain is None or identity == company_domain(domain)
    except (ValueError, UnicodeError):
        return False
