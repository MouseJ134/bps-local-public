#!/usr/bin/env python3
"""Loopback-only tool-contract adapter for a configured local Responses server.

Caller credentials are verified locally and replaced with a separately
configured upstream key. Credential paths are explicitly supplied. No request
or response bodies or credential values are logged. Authenticated WebSocket
requests receive 426; configure the client to use HTTP/SSE.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import sys
import time

from catalog_compat import add_catalog_guard
from error_observer import SSEErrorObserver

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TCPConnector, web
from multidict import CIMultiDict
from yarl import URL


UPSTREAM_BASE = "http://127.0.0.1:8317"
MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_AUTH_HEADER_BYTES = 32 * 1024
ALLOWED_METHODS = {
    "/v1/models": frozenset({"GET", "HEAD"}),
    "/v1/responses": frozenset({"POST", "GET"}),
    "/v1/responses/compact": frozenset({"POST"}),
}
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})
# Remove alternate credential/account headers as well as the four standard
# identity headers, so official credentials cannot be forwarded in a second
# authentication slot. Prefix filtering is a further fail-closed boundary.
PRIVATE_REQUEST_HEADERS = frozenset({
    "authorization", "cookie", "cookie2", "chatgpt-account-id",
    "openai-organization", "openai-project", "openai-account-id",
    "openai-actor-authorization", "x-openai-account-id", "x-chatgpt-account-id",
    "x-openai-organization", "x-openai-project", "x-api-key", "api-key",
    "x-auth-token", "x-authorization", "x-openai-authorization",
    "host", "content-length", "forwarded", "x-forwarded-for",
    "x-forwarded-host", "x-forwarded-proto", "origin", "referer",
})


@dataclass(frozen=True)
class Settings:
    listen_port: int
    client_auth_file: Path
    upstream_key_file: Path
    idle_timeout: float = 300.0
    upstream_base: str = UPSTREAM_BASE
    policy_file: Path | None = None

    def __post_init__(self):
        if type(self.listen_port) is not int or not 1024 <= self.listen_port <= 65535:
            raise ValueError("Listen port must be an unprivileged port")
        if not isinstance(self.idle_timeout, (int, float)) or not 1 <= self.idle_timeout <= 3600:
            raise ValueError("Stream idle timeout must be between 1 and 3600 seconds")
        validated = validate_upstream_base(self.upstream_base, self.listen_port)
        object.__setattr__(self, "upstream_base", validated)


def validate_upstream_base(value: str, listen_port: int) -> str:
    """Allow only a fixed numeric loopback origin, never a general-purpose proxy."""
    try:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError
        url = URL(value)
        if (url.scheme not in {"http", "https"} or url.raw_user is not None
                or url.raw_password is not None or url.raw_path not in ("", "/")
                or url.query_string or url.fragment or not url.raw_host):
            raise ValueError
        host = ipaddress.ip_address(url.raw_host)
        if not host.is_loopback or url.port is None or not 1 <= url.port <= 65535:
            raise ValueError
        # Avoid proxying straight back to this listener (including localhost aliases).
        if url.port == listen_port:
            raise ValueError
        return str(url.origin())
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Upstream must be a different numeric loopback HTTP(S) origin") from None


SETTINGS_KEY = web.AppKey("settings", Settings)
SESSION_KEY = web.AppKey("session", ClientSession)


def emit(event: dict) -> None:
    """All callers must provide only explicitly selected, non-sensitive fields."""
    print(json.dumps(event, separators=(",", ":"), ensure_ascii=True), flush=True)


def error_response(status: int, code: str) -> web.Response:
    return web.json_response(
        {"error": {"type": "local_bridge_error", "code": code}},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def local_request_allowed(request: web.Request, settings: Settings) -> bool:
    """Validate actual TCP peer, Host and browser Origin; never trust forwarding headers."""
    try:
        if not ipaddress.ip_address(request.remote or "").is_loopback:
            return False
    except ValueError:
        return False
    hosts = {f"127.0.0.1:{settings.listen_port}", f"localhost:{settings.listen_port}"}
    if request.headers.get("Host", "").strip().lower() not in hosts:
        return False
    origin = request.headers.get("Origin")
    if origin is not None and origin.rstrip("/").lower() not in {"http://" + host for host in hosts}:
        return False
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return False
    # Reject absolute-form proxy requests instead of acting as an open proxy.
    return request.raw_path.startswith("/") and not request.raw_path.startswith("//")


def read_auth_candidates(path: Path) -> list[str]:
    """Re-read the explicit client credential file; never read refresh/id tokens."""
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Oversized auth file")
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError("Unexpected auth shape")
    tokens = document.get("tokens")
    candidates = []
    token = document.get("token")
    if isinstance(token, str) and token.strip():
        candidates.append(token.strip())
    if isinstance(tokens, dict):
        access = tokens.get("access_token")
        if isinstance(access, str) and access.strip():
            candidates.append(access.strip())
    api_key = document.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key.strip():
        candidates.append(api_key.strip())
    if not candidates:
        raise ValueError("No supported credential")
    return candidates


def authenticate(header: str, candidates: list[str]) -> bool:
    if len(header) > MAX_AUTH_HEADER_BYTES:
        return False
    scheme, separator, supplied = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied.strip():
        return False
    digest = hashlib.sha256(supplied.strip().encode("utf-8")).digest()
    # Compare every eligible candidate, using fixed-size digests and constant-time
    # equality. Do not short-circuit on the first match.
    matches = 0
    for candidate in candidates:
        matches |= int(hmac.compare_digest(digest, hashlib.sha256(candidate.encode("utf-8")).digest()))
    return bool(matches)


def read_upstream_key(path: Path) -> str:
    if path.stat().st_size > MAX_AUTH_HEADER_BYTES:
        raise ValueError("Oversized upstream key file")
    value = path.read_text(encoding="utf-8-sig").strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("Invalid upstream credential")
    return value


def connection_tokens(headers) -> set[str]:
    return {token.strip().lower() for value in headers.getall("Connection", [])
            for token in value.split(",") if token.strip()}


def upstream_headers(headers, key: str) -> CIMultiDict:
    denied = HOP_BY_HOP | PRIVATE_REQUEST_HEADERS | connection_tokens(headers)
    result = CIMultiDict()
    for name, value in headers.items():
        lower = name.lower()
        if lower in denied or lower.startswith("sec-websocket-"):
            continue
        if "authorization" in lower or lower.endswith("api-key") or lower.endswith("auth-token"):
            continue
        result.add(name, value)
    result["Authorization"] = "Bearer " + key
    return result


def downstream_headers(headers) -> CIMultiDict:
    denied = HOP_BY_HOP | connection_tokens(headers) | {
        "set-cookie", "set-cookie2", "authorization", "proxy-authorization",
        "access-control-allow-origin", "access-control-allow-credentials",
        "access-control-allow-headers", "access-control-allow-methods",
    }
    result = CIMultiDict()
    for name, value in headers.items():
        if name.lower() not in denied:
            result.add(name, value)
    result["Cache-Control"] = "no-store"
    return result


async def handle(request: web.Request) -> web.StreamResponse:
    settings = request.app[SETTINGS_KEY]
    start = time.monotonic()
    status = 500
    received = 0
    sent = 0
    # Never log unknown paths: they could themselves contain secret material.
    safe_path = request.path if request.path in ALLOWED_METHODS else "[not-allowed]"
    safe_method = request.method if request.method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} else "[not-allowed]"
    transport = "websocket" if request.headers.get("Upgrade", "").lower() == "websocket" else "http"
    prepared_response = None
    catalog_guard_added = 0
    catalog_diagnostics = {}
    error_observer = None
    observer_fault = False
    try:
        if not local_request_allowed(request, settings):
            status = 403
            return error_response(status, "loopback_origin_required")
        if request.path not in ALLOWED_METHODS:
            status = 404
            return error_response(status, "path_not_allowed")
        if request.method not in ALLOWED_METHODS[request.path]:
            status = 405
            return error_response(status, "method_not_allowed")
        try:
            candidates = read_auth_candidates(settings.client_auth_file)
        except (OSError, ValueError, UnicodeError):
            status = 503
            return error_response(status, "client_auth_unavailable")
        if not authenticate(request.headers.get("Authorization", ""), candidates):
            status = 401
            return error_response(status, "invalid_client_credential")
        # Authenticate before returning 426. This is a deliberate transport
        # negotiation response, not an authentication bypass or a silent retry.
        if transport == "websocket":
            status = 426
            return error_response(status, "use_http_sse")
        if request.path == "/v1/responses" and request.method != "POST":
            status = 405
            return error_response(status, "method_not_allowed")
        try:
            upstream_key = read_upstream_key(settings.upstream_key_file)
        except (OSError, ValueError, UnicodeError):
            status = 503
            return error_response(status, "upstream_auth_unavailable")
        # AppRunner disables request auto-decompression, preserving encoded body
        # bytes until the explicitly scoped compatibility step below. aiohttp
        # enforces MAX_BODY_BYTES for fixed and chunked bodies.
        body = await request.read()
        received = len(body)
        raw_query = request.raw_path.partition("?")[2]
        target = URL(settings.upstream_base + request.path + ("?" + raw_query if raw_query else ""), encoded=True)
        headers = upstream_headers(request.headers, upstream_key)
        # Prompt-only guard: call/result identities and all existing input
        # entries are preserved. No historical context or missing-call repair.
        body, catalog_guard_added = add_catalog_guard(
            body, request.headers, request.method, request.path,
            max_body_bytes=MAX_BODY_BYTES, diagnostics=catalog_diagnostics,
            policy_path=settings.policy_file,
        )
        if catalog_guard_added:
            headers.popall("Content-Encoding", None)
            headers.popall("Content-Length", None)
        async with request.app[SESSION_KEY].request(
            request.method, target, data=body if body else None,
            headers=headers, allow_redirects=False,
        ) as upstream:
            if 300 <= upstream.status < 400:
                status = 502
                return error_response(status, "upstream_redirect_blocked")
            status = upstream.status
            # Observe only eligible, unencoded SSE. Bytes sent to the client are
            # unchanged; diagnostic failures must never alter forwarding.
            if (catalog_diagnostics.get("catalog_count", 0) > 0
                    and len(catalog_diagnostics.get("catalog_hash", "")) == 64
                    and catalog_diagnostics.get("task_profile") in {"generic", "study_notes", "software_development", "document_creation"}
                    and upstream.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "text/event-stream"
                    and len(upstream.headers.getall("Content-Encoding", [])) <= 1
                    and upstream.headers.get("Content-Encoding", "").strip().lower() in ("", "identity")):
                error_observer = SSEErrorObserver()
            prepared_response = web.StreamResponse(status=status, headers=downstream_headers(upstream.headers))
            await prepared_response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await prepared_response.write(chunk)
                sent += len(chunk)
                if error_observer is not None:
                    try:
                        error_observer.feed(chunk)
                    except Exception:
                        error_observer = None
                        observer_fault = True
            await prepared_response.write_eof()
            return prepared_response
    except web.HTTPRequestEntityTooLarge:
        status = 413
        return error_response(status, "request_too_large")
    except asyncio.CancelledError:
        status = 499
        raise
    except (ConnectionResetError, BrokenPipeError):
        status = 499
        if prepared_response is not None:
            prepared_response.force_close()
            return prepared_response
        return error_response(status, "client_disconnected")
    except (ClientError, asyncio.TimeoutError, OSError):
        if prepared_response is not None and prepared_response.prepared:
            # Once SSE headers have been written, never append a JSON error to
            # the stream. Close the transport so Codex observes interruption.
            prepared_response.force_close()
            if request.transport is not None:
                request.transport.close()
            return prepared_response
        status = 502
        return error_response(status, "upstream_unavailable")
    finally:
        error_diagnostics = {}
        if error_observer is not None:
            try:
                error_diagnostics = error_observer.finish()
            except Exception:
                observer_fault = True
        emit({"event": "request", "method": safe_method, "path": safe_path,
              "transport": transport, "status": status, "request_bytes": received,
              "response_bytes": sent,
              "catalog_guard_added": catalog_guard_added,
              "tool_contract_reason": catalog_diagnostics.get("reason", "not_evaluated"),
              "tool_catalog_count": catalog_diagnostics.get("catalog_count", 0),
              "tool_catalog_hash": catalog_diagnostics.get("catalog_hash", ""),
              "task_profile": catalog_diagnostics.get("task_profile", ""),
              "upstream_error_diagnostics": error_diagnostics,
              "diagnostic_observer_fault": observer_fault,
              "duration_ms": round((time.monotonic() - start) * 1000)})


async def session_context(app: web.Application):
    settings = app[SETTINGS_KEY]
    timeout = ClientTimeout(total=None, sock_connect=10, sock_read=settings.idle_timeout)
    async with ClientSession(
        connector=TCPConnector(limit=32, limit_per_host=32), timeout=timeout,
        trust_env=False, auto_decompress=False,
        cookie_jar=DummyCookieJar(),
        skip_auto_headers={"Accept-Encoding", "User-Agent"},
    ) as session:
        app[SESSION_KEY] = session
        yield


def create_app(settings: Settings) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[SETTINGS_KEY] = settings
    app.cleanup_ctx.append(session_context)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-port", type=int, default=os.environ.get("BPS_ADAPTER_LISTEN_PORT", "8319"))
    parser.add_argument("--upstream-base", default=os.environ.get("BPS_ADAPTER_UPSTREAM_BASE", UPSTREAM_BASE))
    client_auth_file = os.environ.get("BPS_ADAPTER_CLIENT_AUTH_FILE")
    upstream_key_file = os.environ.get("BPS_ADAPTER_UPSTREAM_KEY_FILE")
    parser.add_argument("--client-auth-file", type=Path, default=client_auth_file,
                        required=not client_auth_file)
    parser.add_argument("--upstream-key-file", type=Path, default=upstream_key_file,
                        required=not upstream_key_file)
    parser.add_argument("--policy-file", type=Path, default=os.environ.get("BPS_ADAPTER_POLICY_FILE"))
    parser.add_argument("--stream-idle-timeout", type=float,
                        default=os.environ.get("BPS_ADAPTER_STREAM_IDLE_TIMEOUT", "300"))
    args = parser.parse_args()
    try:
        settings = Settings(args.listen_port, args.client_auth_file, args.upstream_key_file,
                            args.stream_idle_timeout, args.upstream_base, args.policy_file)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        read_auth_candidates(settings.client_auth_file)
        read_upstream_key(settings.upstream_key_file)
    except (OSError, ValueError, UnicodeError) as exc:
        emit({"event": "startup_failed", "reason": "credential_file_unavailable", "error_type": type(exc).__name__})
        return 1
    # aiohttp's default access/error logs may include raw URLs. Only our selected
    # structured counters are emitted, including for malformed/unhandled input.
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
    emit({"event": "starting", "listen_host": "127.0.0.1", "listen_port": args.listen_port,
          "upstream": settings.upstream_base, "websocket_behavior": "authenticated_426_http_sse_fallback"})
    try:
        web.run_app(create_app(settings), host="127.0.0.1", port=args.listen_port,
                    access_log=None, print=None, auto_decompress=False, shutdown_timeout=10,
                    handler_cancellation=True)
    except (OSError, RuntimeError) as exc:
        emit({"event": "server_failed", "error_type": type(exc).__name__})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
