"""Bounded, read-only summaries of declared client tool contracts.

This module never executes a tool, invents an alias, or infers a carrier mode
from code/cmd parameter names. The upstream catalog declares carrier modes.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json

MAX_TOOL_DECLARATIONS = 2048
MAX_TOOLS = 1024
MAX_SCHEMA_BYTES = 2 * 1024 * 1024
MAX_VALUE_DEPTH = 64
MAX_VALUE_NODES = 200000
MAX_NAMESPACE_DEPTH = 8
MAX_NAME_BYTES = 256
ANNOTATIONS = frozenset({"description", "title", "$comment"})
SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
SCHEMA_SINGLE = frozenset({"additionalProperties", "unevaluatedProperties", "propertyNames", "contains", "not", "if", "then", "else", "additionalItems", "unevaluatedItems", "contentSchema"})
SCHEMA_ARRAYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})


class CatalogError(ValueError):
    """The fixed reason contains no user text or tool arguments."""


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _check_value(value):
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > MAX_VALUE_DEPTH or count > MAX_VALUE_NODES:
            raise CatalogError("catalog_structure_limit")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)


def canonicalize_schema(schema):
    """Ignore only schema-node prose; preserve parameter names and literals.

    In particular, enum/const/default and unknown keywords are literal JSON.
    Named maps preserve ALL their keys, even description/title/default.
    """
    _check_value(schema)
    if not isinstance(schema, (dict, bool)):
        raise CatalogError("invalid_schema")

    def node(value):
        if isinstance(value, bool):
            return value
        if not isinstance(value, dict):
            raise CatalogError("invalid_schema")
        out = {}
        for key, item in value.items():
            if key in ANNOTATIONS:
                continue
            if key in SCHEMA_MAPS:
                if not isinstance(item, dict):
                    raise CatalogError("invalid_schema")
                out[key] = {name: node(child) for name, child in item.items()}
            elif key in SCHEMA_SINGLE:
                out[key] = node(item)
            elif key in SCHEMA_ARRAYS:
                if not isinstance(item, list):
                    raise CatalogError("invalid_schema")
                out[key] = [node(child) for child in item]
            elif key == "items":
                out[key] = [node(child) for child in item] if isinstance(item, list) else node(item)
            elif key == "dependencies":
                if not isinstance(item, dict):
                    raise CatalogError("invalid_schema")
                out[key] = {name: node(child) if isinstance(child, (dict, bool)) else child for name, child in item.items()}
            else:
                # Preserve enum, const, default, required, $ref, and extensions.
                out[key] = item
        return out

    return node(schema)


@dataclass(frozen=True)
class CatalogSummary:
    # Canonical contracts are suitable for sanitized offline inventories.
    contracts: tuple[dict, ...]
    count: int
    digest: str
    # Server-hosted declarations are metadata, never client tool capabilities.
    hosted_types: tuple[str, ...] = ()


def _has_additional_tools(value):
    """Detect unsupported history-envelope shapes, never parse text as JSON."""
    pending = [(value, 0)]
    seen = 0
    while pending:
        item, depth = pending.pop()
        seen += 1
        if depth > MAX_VALUE_DEPTH or seen > MAX_VALUE_NODES:
            raise CatalogError("history_structure_limit")
        if isinstance(item, dict):
            if item.get("type") == "additional_tools" or "additional_tools" in item:
                return True
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return False


def collect_tool_contracts(document):
    """Merge root and explicitly supported top-level input tool declarations.

    Same qualified name plus the same canonical contract is accepted. Conflicts
    skip the reminder rather than guessing opaque upstream override precedence.
    """
    if not isinstance(document, dict) or not isinstance(document.get("input"), list):
        raise CatalogError("invalid_request_shape")
    if "additional_tools" in document:
        raise CatalogError("unsupported_additional_tools")
    groups = []
    if "tools" in document:
        groups.append(document["tools"])
    for item in document["input"]:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            if _has_additional_tools({k: v for k, v in item.items() if k not in ("type", "tools")}):
                raise CatalogError("unsupported_additional_tools")
            groups.append(item.get("tools"))
        elif _has_additional_tools(item):
            raise CatalogError("unsupported_additional_tools")

    by_name = {}
    serialized = {}
    namespace_identities = {}
    leaf_identities = {}
    hosted_types = set()
    declared = 0
    schema_bytes = 0

    def visit(tools, prefix="", depth=0, namespace_path=()):
        nonlocal declared, schema_bytes
        if not isinstance(tools, list):
            raise CatalogError("invalid_catalog")
        if depth > MAX_NAMESPACE_DEPTH:
            raise CatalogError("catalog_structure_limit")
        for tool in tools:
            declared += 1
            if declared > MAX_TOOL_DECLARATIONS:
                raise CatalogError("catalog_tool_limit")
            if not isinstance(tool, dict):
                raise CatalogError("invalid_catalog")
            kind = tool.get("type")
            name = tool.get("name")
            if not prefix and depth == 0 and kind in ("web_search", "web_search_preview"):
                # Known root hosted declarations are not dispatched client tools.
                hosted_types.add(kind)
                continue
            if kind not in ("function", "custom", "namespace"):
                # Do not infer availability or semantics of native/server tools.
                raise CatalogError("unsupported_catalog_kind")
            if not isinstance(name, str) or not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise CatalogError("invalid_catalog_name")
            qualified = prefix + name
            if len(qualified.encode("utf-8")) > MAX_NAME_BYTES:
                raise CatalogError("catalog_name_limit")
            if kind == "namespace":
                identity = namespace_path + (name,)
                if qualified in by_name or (qualified in namespace_identities and namespace_identities[qualified] != identity):
                    raise CatalogError("conflicting_catalog_identity")
                namespace_identities[qualified] = identity
                visit(tool.get("tools"), qualified + ".", depth + 1, identity)
                continue
            identity = (namespace_path, name)
            if qualified in namespace_identities or (qualified in leaf_identities and leaf_identities[qualified] != identity):
                raise CatalogError("conflicting_catalog_identity")
            if kind == "function":
                if "inputSchema" in tool or "input_schema" in tool:
                    raise CatalogError("unsupported_schema_alias")
                schema = tool.get("parameters", {})
                _check_value(schema)
                schema_bytes += len(_json_bytes(schema))
                strict = tool.get("strict")
                if strict is not None and not isinstance(strict, bool):
                    raise CatalogError("invalid_catalog")
                contract = {"name": qualified, "type": kind, "parameters": canonicalize_schema(schema), "strict": strict}
            else:
                fmt = tool.get("format", {"type": "text"})
                _check_value(fmt)
                if not isinstance(fmt, dict) or fmt.get("type") not in ("text", "grammar"):
                    raise CatalogError("invalid_custom_format")
                schema_bytes += len(_json_bytes(fmt))
                contract = {"name": qualified, "type": kind, "format": fmt}
            if schema_bytes > MAX_SCHEMA_BYTES:
                raise CatalogError("catalog_schema_limit")
            encoded = _json_bytes(contract)
            if qualified in by_name:
                if serialized[qualified] != encoded:
                    raise CatalogError("conflicting_catalog")
            else:
                if len(by_name) >= MAX_TOOLS:
                    raise CatalogError("catalog_tool_limit")
                by_name[qualified] = contract
                serialized[qualified] = encoded
                leaf_identities[qualified] = identity

    for group in groups:
        visit(group)
    if not by_name:
        raise CatalogError("empty_catalog")
    contracts = tuple(by_name[name] for name in sorted(by_name))
    digest = hashlib.sha256(_json_bytes(contracts)).hexdigest()
    return CatalogSummary(contracts, len(contracts), digest, tuple(sorted(hosted_types)))
