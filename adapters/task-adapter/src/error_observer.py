"""Bounded, best-effort SSE error diagnostics with no raw-content output.

The HTTP caller owns forwarding and must skip this observer for compressed or
non-SSE bodies. This module does not mutate chunks, log, connect, retry, or run
tools. Only error envelopes are inspected. ``finish`` accepts a complete JSON
event at EOF even if the final blank line is absent; incomplete JSON is skipped.
"""
from __future__ import annotations

import json
import re

MAX_EVENT_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 8 * 1024
MAX_ERROR_BUCKETS = 32
MAX_TOOL_NAMES = 1024
MAX_COUNTER = 2**31 - 1

CATEGORIES = frozenset({
    "native_not_in_catalog", "tool_outside_client_catalog", "schema_or_resource",
    "missing_tool_call", "transport_invalid", "request_unsupported", "unknown_error",
})
CODES = frozenset({
    "basispoints_protocol_error", "bps_request_unsupported", "bps_incomplete_response",
    "bps_transport_failed_no_replay", "bps_upstream_error", "basispoints_upstream_error",
    "bps_invalid_json", "unknown",
})
TRANSPORTS = frozenset({"run_officejs", "FUNCTION", "FUNCTION_CODE", "FUNCTION_CMD", "CUSTOM"})
DEFAULT_TOOL_NAMES = frozenset({
    "update_plan", "list_connectors", "run_officejs",
    "functions.update_plan", "functions.list_connectors", "functions.run_officejs",
})
_ERROR_EVENTS = frozenset({"response.failed", "error"})
_LINE_END = re.compile(rb"\r\n|\r|\n")
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z", re.ASCII)
_FIELDS = re.compile(r"(?:^|;)[ \t]*(tool|transport|diag|reason|path)=([^;]*)", re.ASCII)
_INDEX = re.compile(r"input\[([0-9]{1,10})\]\Z", re.ASCII)
_INTEGER = re.compile(r"[0-9]{1,10}\Z", re.ASCII)
_SCHEMA_PREFIXES = (
    "basispoints function arguments do not satisfy the declared tool schema or validation resource limits",
    "basispoints native update_plan does not satisfy the declared client argument schema",
    "basispoints tool schema or arguments exceed the nesting limit",
    "basispoints tool number exceeds the exponent limit",
    "basispoints tool number exceeds the precision limit",
    "basispoints tool numbers exceed the validation budget",
)
_TRANSPORT_PREFIXES = (
    "basispoints tool transport remains invalid after ",
    "basispoints tool transport code must contain ",
    "basispoints function code transport ",
    "basispoints function cmd transport ",
    "basispoints raw custom transport ",
    "basispoints raw transport requires ",
    "basispoints custom tools require input text",
)


def _increment(value: int) -> int:
    return min(value + 1, MAX_COUNTER)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non_json_constant")


def _safe_integer(value):
    if not isinstance(value, str) or _INTEGER.fullmatch(value) is None:
        return None
    parsed = int(value)
    return parsed if parsed <= MAX_COUNTER else None


def _tool_name_shape(value):
    if type(value) is not str or not 1 <= len(value) <= 256 or not value.isascii():
        return None
    return value if _TOOL_NAME.fullmatch(value) is not None else None


def _validated_tool_names(extra_names):
    # Never consume arbitrary or potentially infinite iterables. Exact built-in
    # containers also prevent user-defined iteration/hash hooks from running.
    if type(extra_names) not in (list, tuple, set, frozenset):
        return DEFAULT_TOOL_NAMES
    if len(extra_names) > MAX_TOOL_NAMES:
        return DEFAULT_TOOL_NAMES
    allowed = set(DEFAULT_TOOL_NAMES)
    for value in extra_names:
        name = _tool_name_shape(value)
        if name is None or name in allowed:
            continue
        if len(allowed) >= MAX_TOOL_NAMES:
            # An oversized supplied catalog fails closed for all extra names.
            return DEFAULT_TOOL_NAMES
        allowed.add(name)
    return frozenset(allowed)


def _safe_tool(value, allowed_names):
    name = _tool_name_shape(value)
    return name if name is not None and name in allowed_names else None


class SSEErrorObserver:
    """Observe chunks without affecting forwarding; emit only safe metadata.

    ``allowed_tool_names=()`` may add exact names from a separately verified
    client catalog. Never populate it from error text, arguments, credentials or
    untrusted request metadata. Only built-in list/tuple/set/frozenset inputs are
    accepted; malformed names are omitted and oversized catalogs fail closed.
    The six fixed public names remain allowed, with at most 1024 total names.
    Syntactically valid but unlisted tool values are never included in output.

    ``feed(chunk: bytes) -> None`` is best-effort and does not raise ordinary
    exceptions. ``finish() -> dict`` is idempotent and returns fresh containers:
    {errors: [{category, code, count, tool?, transport?, input_index?, diag?}],
     error_events: int, skipped_events: int, overflow_errors: int}.
    """

    def __init__(self, allowed_tool_names=()):
        self._allowed_tool_names = _validated_tool_names(allowed_tool_names)
        self._line = bytearray()
        self._data = []
        self._event_name = None
        self._event_bytes = 0
        self._line_has_content = False
        self._discard = False
        self._pending_cr = False
        self._first_line = True
        self._finished = False
        self._errors = []
        self._buckets = {}
        self._error_events = 0
        self._skipped_events = 0
        self._overflow_errors = 0

    def feed(self, chunk: bytes) -> None:
        if self._finished:
            return
        try:
            if not isinstance(chunk, bytes):
                self._skipped_events = _increment(self._skipped_events)
                return
            if not chunk:
                return
            start = 0
            if self._pending_cr:
                self._pending_cr = False
                if chunk.startswith(b"\n"):
                    self._end_line(2)
                    start = 1
                else:
                    self._end_line(1)
            for match in _LINE_END.finditer(chunk, start):
                self._append(chunk, start, match.start())
                start = match.end()
                if match.group() == b"\r" and start == len(chunk):
                    # Defer dispatch until CR vs CRLF is known, so both bytes
                    # are charged to the same event even at a chunk boundary.
                    self._pending_cr = True
                else:
                    self._end_line(len(match.group()))
            self._append(chunk, start, len(chunk))
        except Exception:
            # Observation never owns the HTTP response. Do not expose the error.
            self._skipped_events = _increment(self._skipped_events)
            self._clear_event()
            self._pending_cr = False

    def finish(self) -> dict:
        if not self._finished:
            try:
                if self._pending_cr:
                    self._pending_cr = False
                    self._end_line(1)
                if self._line_has_content:
                    self._end_line(0)
                if self._discard or self._data:
                    self._dispatch()
            except Exception:
                self._skipped_events = _increment(self._skipped_events)
            finally:
                self._clear_event()
                self._pending_cr = False
                self._finished = True
        return {
            "errors": [dict(item) for item in self._errors],
            "error_events": self._error_events,
            "skipped_events": self._skipped_events,
            "overflow_errors": self._overflow_errors,
        }

    def _append(self, chunk, start, end):
        size = end - start
        if size <= 0:
            return
        self._line_has_content = True
        self._budget(size)
        if not self._discard:
            self._line.extend(chunk[start:end])

    def _budget(self, size):
        self._event_bytes = min(self._event_bytes + size, MAX_EVENT_BYTES + 1)
        if self._event_bytes > MAX_EVENT_BYTES:
            self._discard = True
            self._line.clear()
            self._data.clear()
            self._event_name = None

    def _end_line(self, newline_bytes):
        self._budget(newline_bytes)
        if not self._line_has_content:
            self._dispatch()
            return
        if not self._discard:
            line = bytes(self._line)
            if self._first_line and line.startswith(b"\xef\xbb\xbf"):
                line = line[3:]
            self._first_line = False
            if not line.startswith(b":"):
                field, separator, value = line.partition(b":")
                if not separator:
                    value = b""
                elif value.startswith(b" "):
                    value = value[1:]
                if field == b"data":
                    self._data.append(value)
                elif field == b"event":
                    # Field values themselves are not retained past this event.
                    self._event_name = value if len(value) <= 32 else b"[unsupported]"
        else:
            self._first_line = False
        self._line.clear()
        self._line_has_content = False

    def _clear_event(self):
        self._line.clear()
        self._data.clear()
        self._event_name = None
        self._event_bytes = 0
        self._line_has_content = False
        self._discard = False

    def _dispatch(self):
        try:
            if self._discard:
                self._skipped_events = _increment(self._skipped_events)
                return
            if not self._data:
                return
            # Ordinary named tool/text events are discarded without JSON parsing.
            if self._event_name not in (None, b"", b"message", b"response.failed", b"error"):
                return
            raw = b"\n".join(self._data)
            if raw == b"[DONE]":
                return
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if not isinstance(document, dict):
                raise ValueError("invalid_error_event")
            named = self._event_name.decode("ascii") if self._event_name else ""
            event_type = document.get("type")
            if named in _ERROR_EVENTS:
                if event_type is not None and event_type != named:
                    raise ValueError("ambiguous_event_type")
                event_type = named
            if event_type not in _ERROR_EVENTS:
                return
            if event_type == "response.failed":
                response = document.get("response")
                error = response.get("error") if isinstance(response, dict) else None
            else:
                error = document.get("error", document)
            if not isinstance(error, dict):
                raise ValueError("invalid_error_object")
            message = error.get("message", "")
            if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise ValueError("error_message_limit")
            self._record(error.get("code"), message)
        except Exception:
            self._skipped_events = _increment(self._skipped_events)
        finally:
            self._clear_event()

    def _record(self, code, message):
        safe_code = code if isinstance(code, str) and code in CODES else "unknown"
        message = message.strip(" ")
        fields = {}
        duplicates = set()
        for match in _FIELDS.finditer(message):
            name, value = match.groups()
            if name in fields:
                duplicates.add(name)
            else:
                fields[name] = value.strip(" ")
        for name in duplicates:
            fields.pop(name, None)
        if message.startswith("basispoints returned an unsupported native tool") and fields.get("reason") == "not_in_client_catalog":
            category = "native_not_in_catalog"
        elif message.startswith("basispoints returned a tool outside the client's catalog"):
            category = "tool_outside_client_catalog"
        elif message.startswith(_SCHEMA_PREFIXES):
            category = "schema_or_resource"
        elif (safe_code == "bps_request_unsupported" or message.startswith("bps_request_unsupported")) and fields.get("reason") == "missing_tool_call":
            category = "missing_tool_call"
        elif message.startswith(_TRANSPORT_PREFIXES):
            category = "transport_invalid"
        elif safe_code == "bps_request_unsupported" or message.startswith("bps_request_unsupported"):
            category = "request_unsupported"
        else:
            category = "unknown_error"
        record = {"category": category, "code": safe_code}
        # Schema/resource failures deliberately have no inferred tool or field.
        if category in {"native_not_in_catalog", "tool_outside_client_catalog"}:
            tool = _safe_tool(fields.get("tool"), self._allowed_tool_names)
            if tool is not None:
                record["tool"] = tool
            if fields.get("transport") in TRANSPORTS:
                record["transport"] = fields["transport"]
            diag = _safe_integer(fields.get("diag"))
            if diag is not None:
                record["diag"] = diag
        elif category in {"missing_tool_call", "request_unsupported"}:
            path = fields.get("path", "")
            match = _INDEX.fullmatch(path)
            index = _safe_integer(match.group(1)) if match else None
            if index is not None:
                record["input_index"] = index
        key = tuple(sorted(record.items()))
        self._error_events = _increment(self._error_events)
        bucket = self._buckets.get(key)
        if bucket is not None:
            self._errors[bucket]["count"] = _increment(self._errors[bucket]["count"])
        elif len(self._errors) < MAX_ERROR_BUCKETS:
            self._buckets[key] = len(self._errors)
            record["count"] = 1
            self._errors.append(record)
        else:
            self._overflow_errors = _increment(self._overflow_errors)
