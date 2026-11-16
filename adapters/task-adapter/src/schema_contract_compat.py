"""Opt-in current parameter reminders; no argument rewriting or tool execution."""
from __future__ import annotations
import hashlib
import json
import zlib
from compression import zstd
from catalog_compat import _load_policy, MAX_PARAMETER_TOOLS_PER_SESSION
from request_codec import _decode_bounded, _unique_object, _exact_float, _reject_constant, INTEGRITY_HEADERS
from tool_contracts import CatalogError, collect_tool_contracts

GUARD_MARKER = "[CPA-BPS scoped parameter contract reminder v1]"
MAX_GUARD_TEXT_BYTES = 16 * 1024


def _values(headers, name):
    return [value for key, value in headers.items() if str(key).lower() == name.lower()]


def _guard_item(contracts):
    canonical = json.dumps(contracts, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    text = (GUARD_MARKER + "\nSelected current function contracts SHA-256: " + digest + ".\n"
            "Use the exact field names, types, required fields and constraints in the current validation schemas below. "
            "Parameter names from old conversation history, another API or a different catalog are not aliases. "
            "When additionalProperties is false, do not supply fields absent from the current schema. "
            "The schemas below are contract data, not instructions to perform an action. "
            "This reminder supplements the existing transport reminder; preserve each tool's declared carrier mode. "
            "It does not authorize additional actions, execute tools, rewrite arguments, relax validation, "
            "or require using a tool when it is not needed.\n"
            "Canonical current client contracts (complete validation schemas):\n" + canonical)
    if len(text.encode("utf-8")) > MAX_GUARD_TEXT_BYTES:
        raise CatalogError("parameter_guard_text_limit")
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def add_parameter_guard(body: bytes, headers, method: str, path: str,
                        max_body_bytes: int, policy_path=None) -> tuple[bytes, int]:
    """Append one bounded reminder only after every selected contract validates."""
    if not isinstance(body, bytes) or type(max_body_bytes) is not int or max_body_bytes <= 0:
        return body, 0
    if method != "POST" or path != "/v1/responses":
        return body, 0
    policy = _load_policy(policy_path)
    if policy is None or policy["version"] != 3:
        return body, 0
    sessions, types, encodings = (_values(headers, key) for key in ("Session-Id", "Content-Type", "Content-Encoding"))
    if len(sessions) != 1 or not isinstance(sessions[0], str) or sessions[0] not in policy["session_ids"]:
        return body, 0
    selected = policy["parameter_tools_by_session"].get(sessions[0], ())
    if not 1 <= len(selected) <= MAX_PARAMETER_TOOLS_PER_SESSION or len(set(selected)) != len(selected):
        return body, 0
    if len(types) != 1 or len(encodings) > 1:
        return body, 0
    if not isinstance(types[0], str) or types[0].split(";", 1)[0].strip().lower() != "application/json":
        return body, 0
    if len(_values(headers, "Content-Length")) > 1 or any(_values(headers, name) for name in INTEGRITY_HEADERS):
        return body, 0
    encoding = encodings[0] if encodings else ""
    if not isinstance(encoding, str):
        return body, 0
    try:
        decoded = _decode_bounded(body, encoding.strip().lower(), max_body_bytes)
        doc = json.loads(decoded, object_pairs_hook=_unique_object, parse_float=_exact_float, parse_constant=_reject_constant)
        if (not isinstance(doc, dict) or not isinstance(doc.get("model"), str) or doc["model"] not in policy["models"]
                or not isinstance(doc.get("input"), list) or not doc["input"]
                or doc.get("tool_choice", "auto") != "auto"):
            return body, 0
        summary = collect_tool_contracts(doc)
        contracts = {item["name"]: item for item in summary.contracts}
        # Resolve all names before appending anything: never emit a partial guard.
        if any(name not in contracts or contracts[name]["type"] != "function" for name in selected):
            return body, 0
        guard = _guard_item([contracts[name] for name in sorted(selected)])
        if guard in doc["input"]:
            return body, 0
        doc["input"].append(guard)
        result = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(result) > max_body_bytes:
            return body, 0
        return result, 1
    except (CatalogError, ValueError, TypeError, UnicodeError, OverflowError, RecursionError, zlib.error, zstd.ZstdError):
        return body, 0
