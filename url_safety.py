"""Strict, proxy-free HTTP transport with per-hop checks and pinned DNS answers."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
from urllib.parse import urljoin, urlsplit, urlunsplit


class UnsafeURL(ValueError):
    """A target cannot be proved safe for public-network fetching."""


def _public_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in ipaddress.ip_network('::ffff:0:0:0/96'):
            ip = ipaddress.IPv4Address(int(ip) & 0xffffffff)
        # Refuse transition mechanisms which can encode an internal IPv4 target.
        elif (ip.sixtofour is not None or ip.teredo is not None
              or ip in ipaddress.ip_network('64:ff9b::/96')
              or ip in ipaddress.ip_network('64:ff9b:1::/48')):
            return False
    return (ip.is_global and not any((ip.is_private, ip.is_loopback,
            ip.is_link_local, ip.is_reserved, ip.is_multicast, ip.is_unspecified)))


def validate_url(url: str) -> tuple[object, list[tuple]]:
    """Resolve every answer, reject unsafe ones, and return addresses to dial unchanged.

    Host policy is an additional veto when available. The strict local floor intentionally
    ignores host opt-outs and proxy settings; DNS failure never delegates resolution remotely.
    """
    try:
        if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url:
            raise UnsafeURL('blocked URL: whitespace, control character, or backslash')
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            raise UnsafeURL('requires an absolute http(s) URL')
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeURL('blocked URL: embedded credentials')
        host = parsed.hostname.encode('idna').decode('ascii').rstrip('.').lower()
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if '%' in host or host in {'metadata.google.internal', 'metadata.goog', 'localhost'} or host.endswith('.localhost'):
            raise UnsafeURL('blocked internal hostname')
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not _public_ip(host):
            raise UnsafeURL('blocked URL: non-public address')
        answers = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if not answers or any(family not in {socket.AF_INET, socket.AF_INET6} or not _public_ip(addr[0])
                              for family, _, _, _, addr in answers):
            raise UnsafeURL('blocked URL: non-public or empty DNS answer')
        try:
            from tools.url_safety import is_safe_url
        except ImportError:
            is_safe_url = None
        if is_safe_url is not None and not is_safe_url(url):
            raise UnsafeURL('blocked URL by Hermes URL safety policy')
        return parsed, answers
    except UnsafeURL:
        raise
    except Exception as exc:
        raise UnsafeURL(f'blocked URL: validation or DNS failed ({type(exc).__name__})') from exc


def _dial(answers: list[tuple], timeout: float) -> socket.socket:
    """Connect directly to a previously validated sockaddr; never resolve a second time."""
    last_error = None
    for family, socktype, proto, _, sockaddr in answers:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError('no usable addresses')


def _connection(parsed, answers: list[tuple], timeout: float):
    cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    conn = cls(parsed.hostname.encode('idna').decode('ascii'), parsed.port, timeout=timeout)
    # HTTPConnection.connect uses this factory; HTTPSConnection then wraps that socket with
    # the original hostname for TLS SNI and certificate verification.
    conn._create_connection = lambda address, timeout=timeout, source_address=None: _dial(answers, timeout)
    return conn


def guarded_fetch(url: str, timeout: int, max_bytes: int, headers: dict[str, str]) -> tuple[bytes, dict, str]:
    """GET a bounded body, validating each of at most ten redirect destinations before dialing.

    Environment proxies are deliberately not used because proxy-side DNS defeats IP pinning.
    """
    for hop in range(11):
        parsed, answers = validate_url(url)
        conn = _connection(parsed, answers, timeout)
        try:
            target = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
            # Encode IRIs while preserving existing escapes and reserved URL characters.
            from urllib.parse import quote
            conn.request('GET', quote(target, safe="/%:@!$&'()*+,;=?"), headers=headers)
            resp = conn.getresponse()
            response_headers = {key.lower(): value for key, value in resp.getheaders()}
            if resp.status in {301, 302, 303, 307, 308}:
                location = response_headers.get('location')
                if not location:
                    raise UnsafeURL('redirect missing Location')
                if hop == 10:
                    raise UnsafeURL('too many redirects')
                url = urljoin(url, location)
                continue
            if resp.status >= 400:
                raise urllib.error.HTTPError(url, resp.status, resp.reason, response_headers, None)
            return resp.read(max_bytes), response_headers, url
        finally:
            conn.close()
    raise UnsafeURL('too many redirects')
