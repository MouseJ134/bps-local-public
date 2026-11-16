"""Opt-in, scoped prompt-only compatibility reminder for BPS client catalogs.

No tool execution, aliasing, automatic retries, schema rewrites or validation
bypass occurs here. Unsupported requests pass through byte-for-byte.
"""
from __future__ import annotations
import json
from pathlib import Path
import zlib
from compression import zstd
from request_codec import _decode_bounded, _unique_object, _exact_float, _reject_constant, INTEGRITY_HEADERS
from tool_contracts import CatalogError, collect_tool_contracts
from task_profiles import PROFILE_NAMES, profile_reminder

POLICY_PATH = Path(__file__).with_name("tool_contract_policy.json")
MAX_POLICY_BYTES = 16384
MAX_PARAMETER_TOOLS_PER_SESSION = 8
_POLICY_CACHE = (None, None)
MAX_GUARD_TEXT_BYTES = 32 * 1024
GUARD_MARKER = "[CPA-BPS shared client tool contract reminder v3]"
PREVIOUS_GUARD_MARKER = "[CPA-BPS shared client tool contract reminder v2]"
LEGACY_GUARD_MARKER = "[CPA-BPS external-client tool compatibility reminder v1]"
LEGACY_GUARD_TEXT = (
    "[CPA-BPS external-client tool compatibility reminder v1]\n"
    "Continue the user's actual task using the client tool catalog supplied by the transport. "
    "This is an external Codex client session. Only tools declared in that client catalog are executable here. "
    "Native spreadsheet tools are not additional client tools. In particular, list_connectors and "
    "functions.list_connectors are absent from this client catalog; attempting either terminates the response "
    "without executing a tool. Do not call them. Choose an appropriate declared client tool instead. "
    "For connected-app discovery, the declared mcp__codex_apps__plugin_management._search_plugins is available; "
    "for local filesystem work, the declared exec_command is available. Use them only when relevant to the task. "
    "Send client tool invocations through run_officejs using exactly the FUNCTION, FUNCTION_CODE, FUNCTION_CMD or CUSTOM "
    "transport declared for the selected catalog tool. The carrier is not an Office.js execution environment. "
    "Do not invent additional tools or treat a blocked call as successful. A normal final answer remains allowed "
    "when no tool is needed. This reminder changes tool transport only, not the user's requested task."
)
LEGACY_GUARD_ITEM = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": LEGACY_GUARD_TEXT}]}

RULES = (
    "Continue the user's task using the current client tool catalog supplied by the transport. "
    "Use exact qualified names, types, required arguments and declared constraints; do not invent aliases or extra fields. "
    "The run_officejs carrier does not execute Office code. Follow the selected tool's actual declared transport; "
    "a code/cmd-looking parameter or a oneOf branch alone does not select a carrier mode. "
    "FUNCTION: put one serialized JSON object {\"name\":\"CATALOG_NAME\",\"arguments\":{...}} in carrier code; "
    "arguments is an object, not a second JSON string, and summary is descriptive. "
    "FUNCTION_CODE: only when explicitly declared, set summary to codex2api.function_code/CATALOG_NAME, "
    "put the exact raw code argument in carrier code, and put all other supplied arguments as one JSON object "
    "in extended_summary ({} if none); exclude code from that object. "
    "FUNCTION_CMD: only when explicitly declared, set summary to codex2api.function_cmd/CATALOG_NAME, "
    "put the exact raw cmd argument in carrier code, and put all other supplied arguments as one JSON object "
    "in extended_summary ({} if none); exclude cmd from that object. "
    "CUSTOM: only for a declared custom tool, set summary to codex2api.custom/CATALOG_NAME and put its exact raw input "
    "in carrier code without a JSON envelope or Markdown fences. CATALOG_NAME includes its exact namespace. "
    "Outer carrier arguments include extended_summary, destructive=false and references=[]. "
    "For CODE/CMD do not JSON-wrap, fence or re-escape the raw payload; correctly JSON-escape the outer carrier arguments. "
    "Never nest run_officejs in code. Invoke one declared client tool at a time and consume its actual returned result "
    "before continuing; do not repeat completed calls or invent a result for a failed call. "
    "Preserve actual call/result pairing and call identifiers; quoted context is not proof of an executed tool. "
    "This reminder does not relax validation, permissions or user intent and does not promise to prevent schema errors. "
    "A normal answer remains allowed when no tool is needed."
)


def _parse_policy(raw):
    """Fail closed for unknown versions/fields/profiles; preserve v1 behavior."""
    if len(raw) > MAX_POLICY_BYTES:
        return None
    try:
        p = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(p, dict):
            return None
        version = p.get("version", 1)
        if type(version) is not int or version not in (1, 2, 3):
            return None
        required = {"enabled", "scope", "session_ids", "models"}
        optional = {"version"} if version == 1 else {"version", "profile_by_session"}
        if version == 3:
            required |= {"version", "parameter_tools_by_session"}
        if not required.issubset(p) or set(p) - required - optional:
            return None
        if version >= 2 and "version" not in p:
            return None
        if p["enabled"] is not True or p["scope"] not in ("session_ids", "all_bps"):
            return None
        for key, limit in (("session_ids", 256), ("models", 32)):
            seq = p[key]
            if not isinstance(seq, list) or len(seq) > limit:
                return None
            if any(not isinstance(v, str) or not v or len(v) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in v) for v in seq):
                return None
            if len(set(seq)) != len(seq):
                return None
        if not p["models"] or (p["scope"] == "session_ids" and not p["session_ids"]):
            return None
        profiles = p.get("profile_by_session", {})
        if not isinstance(profiles, dict) or len(profiles) > 256:
            return None
        # Mapping is an explicit opt-in for configured session identities only.
        if any(session not in p["session_ids"] or not isinstance(profile, str) or profile not in PROFILE_NAMES
               for session, profile in profiles.items()):
            return None
        parameter_tools = p.get("parameter_tools_by_session", {})
        if not isinstance(parameter_tools, dict) or len(parameter_tools) > 256:
            return None
        for session, names in parameter_tools.items():
            if session not in p["session_ids"] or not isinstance(names, list) or not 1 <= len(names) <= MAX_PARAMETER_TOOLS_PER_SESSION:
                return None
            if any(not isinstance(name, str) or not name or len(name.encode("utf-8")) > 256
                   or any(ord(c) < 32 or ord(c) == 127 for c in name) for name in names):
                return None
            if len(set(names)) != len(names):
                return None
        return {"version": version, "scope": p["scope"], "session_ids": frozenset(p["session_ids"]),
                "models": frozenset(p["models"]), "profile_by_session": dict(profiles),
                "parameter_tools_by_session": {session: tuple(names) for session, names in parameter_tools.items()}}
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _load_policy(path=None):
    global _POLICY_CACHE
    path = POLICY_PATH if path is None else Path(path)
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        if key == _POLICY_CACHE[0]:
            return _POLICY_CACHE[1]
        if stat.st_size > MAX_POLICY_BYTES:
            policy = None
        else:
            with path.open("rb") as handle:
                raw = handle.read(MAX_POLICY_BYTES + 1)
            policy = _parse_policy(raw)
        _POLICY_CACHE = (key, policy)
        return policy
    except (OSError, ValueError):
        _POLICY_CACHE = (None, None)
        return None


def _guard_item(summary, task_profile="generic"):
    # Names/types come only from the validated merged client contracts. Never
    # infer FUNCTION_CODE/FUNCTION_CMD from parameter names or schema branches.
    inventory = [{"name": item["name"], "type": item["type"]} for item in summary.contracts]
    lookups = {leaf: [item["name"] for item in inventory if item["name"].rsplit(".", 1)[-1] == leaf]
               for leaf in ("update_plan", "list_connectors")}
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    availability = []
    for leaf, names in lookups.items():
        if names:
            availability.append(leaf + " is declared at these exact qualified names: " + compact(names) + ". No aliases are created.")
        else:
            availability.append(leaf + " is not declared in this client catalog; do not call an invented alias.")
    if not lookups["update_plan"]:
        availability.append("When planning helps, describe the steps in ordinary text.")
    text = (GUARD_MARKER + "\n" + "Catalog contracts: " + str(summary.count) +
            "; SHA-256: " + summary.digest + ".\n" + RULES + "\n" +
            "Upstream native tool declarations and descriptions are not evidence of executable local client capabilities. "
            "Do not fabricate capabilities by copying empty tool shells or placeholder declarations; never report fake success. "
            "Keep actual errors visible and use only the currently declared client tools for tool execution.\n" +
            "Task profile: " + task_profile + ". " + profile_reminder(task_profile) + "\n" +
            " ".join(availability) + "\n" +
            "Complete client tool names and types (JSON; function/custom only; this list does not select a carrier mode):\n" +
            compact(inventory))
    if len(text.encode("utf-8")) > MAX_GUARD_TEXT_BYTES:
        raise CatalogError("guard_text_size_limit")
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def _existing_guard(items):
    for item in items:
        if item == LEGACY_GUARD_ITEM:
            return "legacy_guard_present"
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "developer":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "input_text" and isinstance(part.get("text"), str):
                if part["text"].startswith(LEGACY_GUARD_MARKER + "\n"):
                    return "legacy_guard_present"
                if part["text"].startswith((GUARD_MARKER + "\n", PREVIOUS_GUARD_MARKER + "\n")):
                    # Never rewrite a historical reminder, even if its digest differs.
                    return "guard_present"
    return None


def add_catalog_guard(body: bytes, headers, method: str, path: str, *, max_body_bytes: int, diagnostics=None, policy_path=None) -> tuple[bytes, int]:
    if isinstance(diagnostics, dict):
        diagnostics.clear()
        diagnostics.update(reason="not_applied", catalog_count=0, catalog_hash="", task_profile="generic")

    def unchanged(reason):
        if isinstance(diagnostics, dict):
            diagnostics["reason"] = reason
        return body, 0

    policy = _load_policy(policy_path)
    if policy is None:
        return unchanged("policy_disabled")
    if method != "POST" or path != "/v1/responses":
        return unchanged("outside_scope")
    session = headers.get("Session-Id", "")
    if not session or (policy["scope"] == "session_ids" and session not in policy["session_ids"]):
        return unchanged("outside_scope")
    task_profile = policy.get("profile_by_session", {}).get(session, "generic")
    if task_profile not in PROFILE_NAMES:
        return unchanged("policy_disabled")
    if isinstance(diagnostics, dict):
        diagnostics["task_profile"] = task_profile
    if headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        return unchanged("unsupported_content_type")
    getall = getattr(headers, "getall", None)
    if getall is not None:
        if any(len(getall(name, [])) != 1 for name in ("Session-Id", "Content-Type")) or len(getall("Content-Encoding", [])) > 1:
            return unchanged("ambiguous_headers")
    if any(name in headers for name in INTEGRITY_HEADERS):
        return unchanged("integrity_protected")
    try:
        decoded = _decode_bounded(body, headers.get("Content-Encoding", "").strip().lower(), max_body_bytes)
        doc = json.loads(decoded, object_pairs_hook=_unique_object, parse_float=_exact_float, parse_constant=_reject_constant)
        if not isinstance(doc, dict) or not isinstance(doc.get("input"), list) or not doc["input"]:
            return unchanged("invalid_request_shape")
        model = doc.get("model")
        if not isinstance(model, str) or model not in policy["models"]:
            return unchanged("outside_model_scope")
        if doc.get("tool_choice", "auto") != "auto":
            return unchanged("tool_choice_not_auto")
        summary = collect_tool_contracts(doc)
        if isinstance(diagnostics, dict):
            diagnostics.update(catalog_count=summary.count, catalog_hash=summary.digest)
        present = _existing_guard(doc["input"])
        if present:
            return unchanged(present)
        doc["input"].append(_guard_item(summary, task_profile))
        result = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(result) > max_body_bytes:
            return unchanged("output_size_limit")
        if isinstance(diagnostics, dict):
            diagnostics["reason"] = "guard_added"
        return result, 1
    except CatalogError as exc:
        return unchanged(str(exc))
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError, zlib.error, zstd.ZstdError):
        return unchanged("unsupported_or_invalid_body")
