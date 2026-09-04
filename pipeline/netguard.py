"""Outbound URL safety checks.

Article links come from RSS feeds — content nobody here controls. A feed can
be compromised, or simply hostile, and every link in it eventually gets
fetched by the extractor.

Two classes of abuse that costs nothing to prevent:

  * non-HTTP schemes. ``urllib.request.urlopen`` honours ``file://`` — this
    was verified, not assumed — so a link of ``file:///.../.env`` would be
    read and could end up quoted in a published summary.
  * requests to private, loopback or link-local addresses. On a CI runner
    ``http://169.254.169.254/`` is the cloud metadata endpoint; on a home
    machine the same trick reaches the router's admin page.

Today's fetch paths happen to be safe by accident — trafilatura rejects
``file://`` and the Google News resolver only accepts news.google.com URLs.
This module makes that a property of the code rather than a coincidence.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}


# RFC 6052 well-known prefix. A DNS64 resolver answers AAAA for an IPv4-only
# host by synthesising an address in this range with the real IPv4 in its low
# 32 bits, and the host then reaches it through a NAT64 gateway.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")


def _unwrap(ip):
    """Resolve an IPv6 address to the IPv4 one it actually stands for.

    Python classifies the whole of 64:ff9b::/96 as `is_reserved`, so on any
    network running DNS64 - IPv6-only networks, most mobile carriers, and
    this developer's own connection as of 2026-08-26 - every public hostname
    resolved to a "reserved" address and the guard blocked the ENTIRE feed
    harvest. The prefix is not the address: what matters is the IPv4 address
    embedded in it, which must then face exactly the same checks.
    """
    if ip.version != 6:
        return ip
    if ip.ipv4_mapped:                       # ::ffff:a.b.c.d
        return ip.ipv4_mapped
    if ip in _NAT64_WELL_KNOWN:              # 64:ff9b::a.b.c.d
        return ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return ip


def _is_blocked_ip(host: str) -> bool:
    """True if the host resolves to somewhere we must not fetch from."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return True                      # cannot resolve -> do not fetch

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        ip = _unwrap(ip)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def check(url: str, resolve_dns: bool = True) -> str | None:
    """Return a reason the URL must not be fetched, or None if it is fine.

    `resolve_dns=False` skips the DNS lookup for callers that only need the
    cheap scheme check — a lookup per link is slow when there are hundreds.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable URL"

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return f"scheme '{parts.scheme}' not allowed"
    if not parts.hostname:
        return "no host"
    if resolve_dns and _is_blocked_ip(parts.hostname):
        return f"host '{parts.hostname}' resolves to a private or local address"
    return None


def is_safe(url: str, resolve_dns: bool = True) -> bool:
    return check(url, resolve_dns) is None
