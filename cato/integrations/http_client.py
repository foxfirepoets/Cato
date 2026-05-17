"""Minimal standard-library HTTP client for integrations."""

from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


_ALLOWED_SCHEMES = ("http", "https")


def _assert_safe_url(url: str) -> None:
    """Defense-in-depth: only http(s), never private/loopback/link-local hosts."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Disallowed URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL missing host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"URL host resolution failed: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"Disallowed URL host (private/internal range): {host}")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: str

    def as_dict(self) -> dict[str, Any]:
        parsed: Any = None
        try:
            parsed = json.loads(self.body) if self.body else None
        except json.JSONDecodeError:
            parsed = None
        return {
            "status": self.status,
            "headers": self.headers,
            "body": parsed if parsed is not None else self.body,
        }


def request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    body_format: str = "json",
    timeout: float = 20.0,
) -> HttpResponse:
    """Issue one HTTP request using urllib only."""
    _assert_safe_url(url)
    payload: bytes | None = None
    request_headers = dict(headers)

    if body is not None:
        if body_format == "form":
            payload = urlencode(body, doseq=True).encode("utf-8")
            request_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded"
            )
        else:
            payload = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

    req = Request(
        url=url,
        data=payload,
        headers=request_headers,
        method=method.upper(),
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return HttpResponse(
                status=int(resp.status),
                headers=dict(resp.headers.items()),
                body=resp.read().decode("utf-8", errors="replace"),
            )
    except HTTPError as exc:
        return HttpResponse(
            status=int(exc.code),
            headers=dict(exc.headers.items()),
            body=exc.read().decode("utf-8", errors="replace"),
        )
    except URLError as exc:
        return HttpResponse(
            status=0,
            headers={},
            body=json.dumps({"error": str(exc.reason)}),
        )
