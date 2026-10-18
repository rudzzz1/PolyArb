"""Production HTTP transport with automatic TLS fragmentation to ensure reliable connectivity.
Handles ISP/firewall DPI resets seamlessly on Windows and restricted networks.
"""
from __future__ import annotations
import json
import os
import random
import socket
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

_DNS_CACHE: dict[str, str] = {}
_DEFAULT_FALLBACK_IPS = {
    "gamma-api.polymarket.com": "172.64.153.51",
    "clob.polymarket.com": "104.18.34.205",
    "external-api.kalshi.com": "3.20.180.5",
}


def _get_fallback_ips() -> dict[str, str]:
    """Returns fallback IP dictionary, overridable via POLYARB_FALLBACK_IPS env var."""
    custom = os.environ.get("POLYARB_FALLBACK_IPS", "")
    res = dict(_DEFAULT_FALLBACK_IPS)
    if custom:
        for pair in custom.split(","):
            if "=" in pair:
                h, ip = pair.split("=", 1)
                res[h.strip()] = ip.strip()
            elif ":" in pair:
                h, ip = pair.split(":", 1)
                res[h.strip()] = ip.strip()
    return res


def _resolve_host(host: str) -> str:
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]
    try:
        ip = socket.gethostbyname(host)
        _DNS_CACHE[host] = ip
        return ip
    except Exception:
        pass
    try:
        addrs = socket.getaddrinfo(host, 443, socket.AF_INET)
        if addrs:
            ip = addrs[0][4][0]
            _DNS_CACHE[host] = ip
            return ip
    except Exception:
        pass
    fallbacks = _get_fallback_ips()
    if host in fallbacks:
        return fallbacks[host]
    return socket.gethostbyname(host)


def _raw_https_get(host: str, path: str, timeout: float = 12.0) -> bytes:
    """Connects to host via port 443 with TLS ClientHello packet splitting to bypass ISP DPI resets."""
    ip = _resolve_host(host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((ip, 443))

    ctx = ssl.create_default_context()
    bio_in = ssl.MemoryBIO()
    bio_out = ssl.MemoryBIO()
    ssl_obj = ctx.wrap_bio(bio_in, bio_out, server_hostname=host)

    try:
        ssl_obj.do_handshake()
    except ssl.SSLWantReadError:
        pass

    # Extract OpenSSL ClientHello and split initial 10 bytes to bypass SNI inspection
    client_hello = bio_out.read()
    sock.send(client_hello[:10])
    time.sleep(0.02)
    sock.send(client_hello[10:])

    while True:
        try:
            ssl_obj.do_handshake()
            break
        except ssl.SSLWantReadError:
            data = sock.recv(4096)
            if not data:
                break
            bio_in.write(data)
            out = bio_out.read()
            if out:
                sock.send(out)

    # Format HTTP GET request
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: {DEFAULT_HEADERS['User-Agent']}\r\n"
        f"Accept: application/json\r\n"
        f"Connection: close\r\n\r\n"
    )
    ssl_obj.write(req.encode("utf-8"))
    sock.send(bio_out.read())

    # Read response
    chunks = []
    while True:
        try:
            chunk = ssl_obj.read(8192)
            if not chunk:
                break
            chunks.append(chunk)
        except ssl.SSLWantReadError:
            data = sock.recv(8192)
            if not data:
                break
            bio_in.write(data)

    sock.close()
    raw = b"".join(chunks)

    # Split HTTP headers and body
    parts = raw.split(b"\r\n\r\n", 1)
    if len(parts) < 2:
        return raw

    headers, body = parts[0], parts[1]
    # Handle Transfer-Encoding: chunked
    if b"transfer-encoding: chunked" in headers.lower():
        body_clean = bytearray()
        offset = 0
        while offset < len(body):
            crlf = body.find(b"\r\n", offset)
            if crlf == -1:
                break
            hex_len = body[offset:crlf].split(b";")[0].strip()
            try:
                chunk_len = int(hex_len, 16)
            except ValueError:
                break
            if chunk_len == 0:
                break
            chunk_data = body[crlf + 2 : crlf + 2 + chunk_len]
            body_clean.extend(chunk_data)
            offset = crlf + 2 + chunk_len + 2
        return bytes(body_clean)

    return body


def http_get(url: str, params: dict | None = None, timeout: float = 12.0, max_retries: int = 3) -> Any:
    """Robust HTTP GET with automatic TLS fragmentation, proxy support, and exponential backoff with jitter."""
    if params:
        query_str = urllib.parse.urlencode(params)
        url += ("&" if "?" in url else "?") + query_str

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    path = parsed.path + ("?" + parsed.query if parsed.query else "")

    # Configurable proxy support (Phase 6.4)
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY")

    # TLS-splitting transport for Polymarket and Kalshi when not behind explicit HTTP proxy
    if not proxy_url and ("polymarket.com" in host or "kalshi.com" in host):
        last_err = None
        for attempt in range(max_retries):
            try:
                raw_bytes = _raw_https_get(host, path, timeout=timeout)
                text = raw_bytes.decode("utf-8", errors="ignore")
                return json.loads(text)
            except Exception as e:
                last_err = e
                # Exponential backoff with random jitter
                backoff = min(2.0, (0.15 * (2 ** attempt)) + random.uniform(0.01, 0.05))
                time.sleep(backoff)
        if last_err:
            raise last_err

    # Standard urllib with proxy and retry support for other endpoints or when proxy is set
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    opener = urllib.request.build_opener()
    if proxy_url:
        opener.add_handler(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))

    last_err = None
    for attempt in range(max_retries):
        try:
            with opener.open(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last_err = e
            backoff = min(2.0, (0.15 * (2 ** attempt)) + random.uniform(0.01, 0.05))
            time.sleep(backoff)

    if last_err:
        raise last_err
