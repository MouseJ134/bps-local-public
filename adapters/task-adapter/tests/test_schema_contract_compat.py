"""Synthetic parameter-contract tests; no private requests or model execution."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from compression import zstd
from multidict import CIMultiDict
import catalog_compat
import schema_contract_compat as guard
from tool_contracts import collect_tool_contracts

SESSION = "synthetic-session"
MODEL = "synthetic-model"
TOOL = "collaboration.followup_task"


def policy():
    return {"version": 3, "enabled": True, "scope": "session_ids", "session_ids": [SESSION],
            "models": [MODEL], "profile_by_session": {SESSION: "generic"},
            "parameter_tools_by_session": {SESSION: [TOOL]}}


def fixture():
    return {"model": MODEL, "stream": True, "tool_choice": "auto", "input": [
        {"type": "message", "role": "user", "content": "Synthetic task."},
        {"type": "function_call", "name": TOOL, "call_id": "synthetic-call", "arguments": '{"agent_name":"old","task":"old"}'},
        {"type": "function_call_output", "call_id": "synthetic-call", "output": "Synthetic prior failure."}],
        "tools": [{"type": "function", "name": TOOL, "strict": True, "parameters": {
            "type": "object", "description": "Prose annotation.",
            "properties": {"target": {"type": "string", "minLength": 1},
                           "message": {"type": "string", "encrypted": True}},
            "required": ["target", "message"], "additionalProperties": False}}]}


def headers():
    return CIMultiDict({"Session-Id": SESSION, "Content-Type": "application/json"})


def encode(doc):
    return json.dumps(doc, ensure_ascii=False, indent=2).encode()


def text(doc):
    return doc["input"][-1]["content"][0]["text"]


class ParameterContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy_file = Path(self.temp.name) / "policy.json"
        self.write_policy(policy())

    def write_policy(self, value):
        self.policy_file.write_bytes(encode(value))
        catalog_compat._POLICY_CACHE = (None, None)

    def apply(self, doc=None, *, raw=None, h=None, method="POST", path="/v1/responses", limit=1024*1024):
        raw = encode(fixture() if doc is None else doc) if raw is None else raw
        out, count = guard.add_parameter_guard(raw, headers() if h is None else h, method, path, limit, self.policy_file)
        return raw, out, count

    def assert_unchanged(self, *args, **kwargs):
        raw, out, count = self.apply(*args, **kwargs)
        self.assertEqual((out, count), (raw, 0))

    def test_current_schema_replaces_no_history_or_arguments(self):
        doc = fixture(); raw, out, count = self.apply(doc)
        changed = json.loads(out)
        self.assertEqual(count, 1)
        self.assertEqual({**changed, "input": changed["input"][:-1]}, doc)
        contracts = list(collect_tool_contracts(doc).contracts)
        canonical = json.dumps(contracts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.assertTrue(text(changed).endswith(canonical))
        self.assertIn(hashlib.sha256(canonical.encode()).hexdigest(), text(changed))
        self.assertIn('"encrypted":true', text(changed))
        self.assertIn('"required":["target","message"]', text(changed))
        self.assertNotIn('agent_name', text(changed))
        self.assertNotIn('Prose annotation.', text(changed))
        self.assertEqual(self.apply(raw=out)[1:], (out, 0))

    def test_existing_catalog_v3_does_not_suppress_parameter_reminder(self):
        doc = fixture()
        doc["input"].append(catalog_compat._guard_item(collect_tool_contracts(doc)))
        _, out, count = self.apply(doc)
        self.assertEqual(count, 1)
        self.assertEqual(json.loads(out)["input"][:-1], doc["input"])

    def test_schema_change_appends_current_guard_preserving_old(self):
        _, out, _ = self.apply()
        doc = json.loads(out); old = copy.deepcopy(doc["input"])
        doc["tools"][0]["parameters"]["properties"]["message"]["minLength"] = 2
        _, updated, count = self.apply(doc)
        self.assertEqual(count, 1)
        self.assertEqual(json.loads(updated)["input"][:-1], old)
        self.assertNotEqual(text(doc), text(json.loads(updated)))
        self.assertEqual(self.apply(raw=updated)[1:], (updated, 0))

    def test_selection_order_does_not_change_digest(self):
        doc = fixture(); extra = copy.deepcopy(doc["tools"][0]); extra["name"] = "other.function"
        doc["tools"].append(extra)
        p = policy(); p["parameter_tools_by_session"][SESSION] = [extra["name"], TOOL]
        self.write_policy(p); _, out, count = self.apply(doc)
        self.assertEqual(count, 1)
        p["parameter_tools_by_session"][SESSION].reverse(); self.write_policy(p)
        self.assertEqual(self.apply(raw=out)[1:], (out, 0))

    def test_version_one_and_two_never_enable_parameter_guard(self):
        for version in (1, 2):
            p = policy(); p["version"] = version; p.pop("parameter_tools_by_session")
            if version == 1: p.pop("profile_by_session")
            self.write_policy(p)
            self.assertIsNotNone(catalog_compat._load_policy(self.policy_file))
            self.assert_unchanged()

    def test_disabled_default_and_actual_disabled_loader(self):
        p = policy(); p["enabled"] = False; self.write_policy(p)
        self.assertIsNone(catalog_compat._load_policy(self.policy_file)); self.assert_unchanged()
        default = Path(guard.__file__).with_name("tool_contract_policy.json")
        self.assertIsNone(catalog_compat._load_policy(default))
        self.assertEqual(json.loads(default.read_text())["parameter_tools_by_session"], {})

    def test_policy_rejects_duplicate_unknown_and_unbounded_config(self):
        mutations = [lambda p: p.update(version=4), lambda p: p.update(unknown=True),
            lambda p: p.pop("parameter_tools_by_session"),
            lambda p: p["parameter_tools_by_session"].update({"unlisted-session": [TOOL]}),
            lambda p: p["parameter_tools_by_session"].update({SESSION: [TOOL, TOOL]}),
            lambda p: p["parameter_tools_by_session"].update({SESSION: []}),
            lambda p: p["parameter_tools_by_session"].update({SESSION: ["x" + str(i) for i in range(9)]}),
            lambda p: p["parameter_tools_by_session"].update({SESSION: ["bad\nname"]}),
            lambda p: p["parameter_tools_by_session"].update({SESSION: ["x" * 257]}),
            lambda p: p["parameter_tools_by_session"].update({SESSION: "function"})]
        for mutate in mutations:
            p = policy(); mutate(p); self.write_policy(p)
            self.assertIsNone(catalog_compat._load_policy(self.policy_file)); self.assert_unchanged()

    def test_old_versions_cannot_opt_in_with_new_field(self):
        for version in (1, 2):
            p = policy(); p["version"] = version; self.write_policy(p)
            self.assertIsNone(catalog_compat._load_policy(self.policy_file)); self.assert_unchanged()

    def test_scope_requires_explicit_session_and_model(self):
        h = headers(); h["Session-Id"] = "unlisted-session"; self.assert_unchanged(h=h)
        doc = fixture(); doc["model"] = "other-model"; self.assert_unchanged(doc)
        p = policy(); p["parameter_tools_by_session"] = {}; self.write_policy(p); self.assert_unchanged()

    def test_all_bps_catalog_scope_still_requires_selected_session(self):
        p = policy(); p["scope"] = "all_bps"; self.write_policy(p)
        self.assertEqual(self.apply()[2], 1)
        h = headers(); h["Session-Id"] = "not-mapped"; self.assert_unchanged(h=h)

    def test_method_path_content_type_and_tool_choice_scope(self):
        for kwargs in ({"method": "GET"}, {"path": "/v1/chat/completions"}, {"path": "/v1/responses?x=1"}):
            self.assert_unchanged(**kwargs)
        h = headers(); h["Content-Type"] = "text/plain"; self.assert_unchanged(h=h)
        for choice in ("none", "required", {"type": "function", "name": TOOL}):
            doc = fixture(); doc["tool_choice"] = choice; self.assert_unchanged(doc)

    def test_unknown_custom_and_conflicting_selected_tools_fail_closed(self):
        doc = fixture(); doc["tools"][0]["name"] = "unknown"; self.assert_unchanged(doc)
        doc = fixture(); doc["tools"] = [{"type": "custom", "name": TOOL}]; self.assert_unchanged(doc)
        doc = fixture(); conflict = copy.deepcopy(doc["tools"][0]); conflict["strict"] = False
        doc["tools"].append(conflict); self.assert_unchanged(doc)
        doc = fixture(); doc["tools"].append({"type": "namespace", "name": "collaboration", "tools": [
            {**copy.deepcopy(doc["tools"][0]), "name": "followup_task"}]})
        self.assert_unchanged(doc)

    def test_every_selected_tool_must_validate_before_any_append(self):
        for second in ("missing", "custom", "conflict"):
            doc = fixture(); p = policy(); p["parameter_tools_by_session"][SESSION].append("other.function")
            if second == "custom": doc["tools"].append({"type": "custom", "name": "other.function"})
            if second == "conflict":
                tool = {**copy.deepcopy(doc["tools"][0]), "name": "other.function"}
                doc["tools"].extend([tool, {**tool, "strict": False}])
            self.write_policy(p); self.assert_unchanged(doc)

    def test_merged_identical_contracts_keep_existing_collector_semantics(self):
        doc = fixture(); doc["input"].append({"type": "additional_tools", "tools": copy.deepcopy(doc["tools"])})
        self.assertEqual(self.apply(doc)[2], 1)

    def test_nested_namespace_and_full_validation_literals_preserved(self):
        doc = fixture(); schema = doc["tools"][0]["parameters"]
        schema["$defs"] = {"description": {"type": "string", "enum": ["description", "title"]}}
        schema["allOf"] = [{"if": {"properties": {"target": {"const": "x"}}}, "then": {"required": ["message"]}}]
        schema["properties"]["message"] = {"$ref": "#/$defs/description", "encrypted": True}
        leaf = doc["tools"][0]; leaf["name"] = "followup_task"
        doc["tools"] = [{"type": "namespace", "name": "collaboration", "tools": [leaf]}]
        _, out, count = self.apply(doc)
        self.assertEqual(count, 1)
        self.assertIn('"$ref":"#/$defs/description"', text(json.loads(out)))
        self.assertIn('"enum":["description","title"]', text(json.loads(out)))

    def test_gzip_and_zstd_decode_and_idempotence(self):
        for encoding, compress in (("gzip", gzip.compress), ("zstd", zstd.compress)):
            h = headers(); h["Content-Encoding"] = encoding
            _, out, count = self.apply(raw=compress(encode(fixture())), h=h)
            self.assertEqual(count, 1)
            compressed = compress(out); self.assert_unchanged(raw=compressed, h=h)
            self.assert_unchanged(raw=b"invalid", h=h)

    def test_integrity_and_ambiguous_headers_preserve_original_bytes(self):
        for name in ("Session-Id", "Content-Type", "Content-Encoding", "Content-Length"):
            h = headers()
            if name not in h: h[name] = "identity" if name == "Content-Encoding" else "1"
            h.add(name, h[name]); self.assert_unchanged(h=h)
        for name in guard.INTEGRITY_HEADERS:
            h = headers(); h[name] = "synthetic"; self.assert_unchanged(h=h)

    def test_case_insensitive_headers(self):
        self.assertEqual(self.apply(h={"session-id": SESSION, "content-type": "application/json; charset=utf-8"})[2], 1)

    def test_body_output_and_total_guard_limits(self):
        raw = encode(fixture()); self.assert_unchanged(raw=raw, limit=20)
        self.assert_unchanged(raw=raw, limit=len(raw))
        doc = fixture(); doc["tools"][0]["parameters"]["enum"] = ["x" * guard.MAX_GUARD_TEXT_BYTES]
        self.assert_unchanged(doc)
        doc = fixture(); p = policy(); names = []
        for index in range(8):
            tool = copy.deepcopy(doc["tools"][0]); tool["name"] = "tool" + str(index)
            tool["parameters"]["enum"] = ["x" * 2200]; names.append(tool["name"])
            doc["tools"].append(tool)
        p["parameter_tools_by_session"][SESSION] = names; self.write_policy(p); self.assert_unchanged(doc)

    def test_invalid_json_duplicate_keys_and_request_shapes(self):
        for raw in (b"not-json", b'{"model":"a","model":"b"}', b"[]"):
            self.assert_unchanged(raw=raw)
        doc = fixture(); doc["input"] = []; self.assert_unchanged(doc)
        doc = fixture(); doc["tools"][0]["parameters"] = 5; self.assert_unchanged(doc)

    def test_missing_policy_preserves_bytes(self):
        self.policy_file.unlink(); self.assert_unchanged()
