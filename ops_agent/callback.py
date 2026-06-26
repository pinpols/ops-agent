"""回调投递(best-effort)+ SSRF/DNS-rebinding 加固的 pinned-TLS HTTP 客户端。

诊断完成后,配了 `OPS_CALLBACK_URL` 就把结果 POST 给下游(成功/失败均回调)。prod 下走
"先解析一次 → 校验公网 IP → 连到该 IP"的 pinned 连接,挡 DNS rebinding;dev/staging 走普通 urllib。
从 server.py 拆出(原 god-module 的第三类职责),与传输层、诊断任务内核解耦。
"""

import ipaddress
import json
import logging
import os
import socket
import ssl
from base64 import b64encode
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

from ops_agent.config import Settings, get_settings

logger = logging.getLogger("ops_agent.callback")


def _post_callback(
    job: Any,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """配了 OPS_CALLBACK_URL 就把结果 POST 过去(成功/失败均回调,best-effort,失败只 warn)。"""
    settings = get_settings()
    url = settings.ops_callback_url
    allowed, reason = _callback_url_allowed(url, settings)
    if not allowed:
        if url:
            logger.warning(
                "回调 URL 被安全策略拒绝 job_id=%s trace_id=%s reason=%s",
                job.id,
                job.trace_id,
                reason,
            )
        return
    if url is None:
        return
    import urllib.request

    body = json.dumps(
        {
            "job_id": job.id,
            "trace_id": job.trace_id,
            "status": status,
            "result": result,
            "error": error,
        }
    ).encode("utf-8")
    try:
        if settings.production:
            _post_https_callback_pinned(url, body)
        else:
            req = urllib.request.Request(  # noqa: S310
                url, data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=10).close()  # noqa: S310  # nosec B310
    except Exception as exc:  # noqa: BLE001 - 回调是 best-effort,失败不该影响诊断结果
        logger.warning(
            "回调投递失败 job_id=%s trace_id=%s url=%s: %s", job.id, job.trace_id, url, exc
        )


def _callback_error(exc: Exception) -> str:
    settings = get_settings()
    if settings.production:
        return f"{type(exc).__name__}: callback_error"
    return f"{type(exc).__name__}: {exc}"


def _callback_url_allowed(url: str | None, settings: Settings) -> tuple[bool, str]:
    if not url:
        return False, "not_configured"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "callback_url_must_be_http_or_https"
    host = parsed.hostname.lower()
    if settings.ops_callback_allow_hosts and host not in settings.ops_callback_allow_hosts:
        return False, "callback_host_not_in_allowlist"
    if settings.production:
        if parsed.scheme != "https":
            return False, "prod_callback_requires_https"
        if not settings.ops_callback_allow_hosts:
            return False, "prod_callback_requires_allowlist"
        ok, reason = _callback_host_is_public(host, parsed.port)
        if not ok:
            return False, reason
    return True, "ok"


def _resolve_callback_public_addresses(
    host: str, port: int | None = None
) -> tuple[list[ipaddress.IPv4Address | ipaddress.IPv6Address], str]:
    try:
        addresses = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return [], "callback_host_dns_failed"
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                return [], "callback_host_unparseable_address"
    if not addresses:
        return [], "callback_host_no_addresses"
    for address in addresses:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return [], "callback_host_resolves_to_non_public_ip"
    return sorted(set(addresses), key=str), "ok"


def _callback_host_is_public(host: str, port: int | None = None) -> tuple[bool, str]:
    addresses, reason = _resolve_callback_public_addresses(host, port)
    return bool(addresses), reason


def _post_https_callback_pinned(url: str, body: bytes, *, timeout: float = 10) -> None:
    """POST HTTPS callback after resolving once, then connecting to that vetted IP.

    urllib validates the URL and then resolves again inside urlopen. In production callbacks this
    keeps DNS rebinding from swapping a previously public host to an internal address between
    policy check and connect. TLS still validates against the original hostname via SNI.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("prod callback must be https with hostname")
    host = parsed.hostname.lower()
    port = parsed.port or 443
    addresses, reason = _resolve_callback_public_addresses(host, port)
    if not addresses:
        raise ValueError(reason)

    request = _build_callback_http_request(parsed, body)

    context = ssl.create_default_context()
    last_error: Exception | None = None
    for address in addresses:
        try:
            with (
                _open_callback_tcp_stream(address, host, port, timeout=timeout) as raw,
                context.wrap_socket(raw, server_hostname=host) as sock,
            ):
                sock.settimeout(timeout)
                sock.sendall(request)
                with sock.makefile("rb") as response:
                    status_line = response.readline(512).decode("iso-8859-1", errors="replace")
                parts = status_line.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    raise RuntimeError("invalid callback response")
                status = int(parts[1])
                if status < 200 or status >= 300:
                    raise RuntimeError(f"callback http status {status}")
                return
        except Exception as exc:  # noqa: BLE001 - try next resolved public address
            last_error = exc
    if last_error:
        raise last_error


def _build_callback_http_request(parsed: Any, body: bytes) -> bytes:
    host = parsed.hostname.lower()
    port = parsed.port or 443
    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {_host_header(host, port)}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body
    return request


def _host_header(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        base = host
    else:
        base = f"[{address}]" if address.version == 6 else str(address)
    return base if port == 443 else f"{base}:{port}"


def _proxy_from_env() -> tuple[str, int, str | None] | None:
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("HTTPS_PROXY 仅支持 http://host:port CONNECT 代理")
    auth = None
    if parsed.username:
        username = unquote(parsed.username)
        password = unquote(parsed.password or "")
        token = b64encode(f"{username}:{password}".encode()).decode("ascii")
        auth = f"Proxy-Authorization: Basic {token}\r\n"
    return parsed.hostname, parsed.port or 8080, auth


def _open_callback_tcp_stream(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    host: str,
    port: int,
    *,
    timeout: float,
):
    proxy = _proxy_from_env()
    if proxy is None:
        return socket.create_connection((str(address), port), timeout=timeout)

    proxy_host, proxy_port, proxy_auth = proxy
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.settimeout(timeout)
    target = _host_header(str(address), port)
    auth_header = proxy_auth or ""
    connect = (
        f"CONNECT {target} HTTP/1.1\r\n"
        f"Host: {target}\r\n"
        f"{auth_header}"
        "Proxy-Connection: Keep-Alive\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        sock.sendall(connect)
        with sock.makefile("rb") as response:
            status_line = response.readline(512).decode("iso-8859-1", errors="replace")
            while response.readline(512).strip():
                pass
        parts = status_line.split()
        if len(parts) < 2 or parts[1] != "200":
            raise RuntimeError("callback proxy CONNECT failed")
        return sock
    except Exception:
        sock.close()
        raise
