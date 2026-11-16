"""Offline v3/profile tests; no tool execution, network, or production writes."""
from __future__ import annotations
import copy
import gzip
import json
from pathlib import Path
import sys
import tempfile
from unittest import TestCase, main, mock
from compression import zstd
from multidict import CIMultiDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import catalog_compat as guard
import request_codec as context
import task_profiles as profiles
from tool_contracts import collect_tool_contracts

ROOT = "synthetic-session-dev"
DEPLOY = "synthetic-session-deploy"
PROBABILITY = "synthetic-session-study"
MODEL = "synthetic-bps-model"
CATALOG_LABEL = "Complete client tool names and types (JSON; function/custom only; this list does not select a carrier mode):\n"


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def function(name="probe", schema=None):
    return {"type": "function", "name": name, "parameters": schema or {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}


def document():
    return {"model": MODEL, "tools": [function()], "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Continue the existing authorized task."}]}]}


def headers(session=ROOT, **extra):
    result = CIMultiDict({"Session-Id": session, "Content-Type": "application/json"})
    result.update(extra)
    return result


def policy_v1():
    return {"enabled": True, "scope": "session_ids", "session_ids": [ROOT, DEPLOY, PROBABILITY], "models": [MODEL]}


def policy_v2():
    return {**policy_v1(), "version": 2, "profile_by_session": {ROOT: "software_development", DEPLOY: "software_development", PROBABILITY: "study_notes"}}


def reminder(body):
    return json.loads(body)["input"][-1]["content"][0]["text"]


class ProfileTests(TestCase):
    def test_only_fixed_known_profiles(self):
        self.assertEqual(profiles.PROFILE_NAMES, frozenset({"generic", "software_development", "study_notes", "document_creation"}))
        for profile in profiles.PROFILE_NAMES:
            self.assertIsInstance(profiles.profile_reminder(profile), str)
        for bad in ("automatic", "", None, [], 2):
            with self.subTest(profile=bad), self.assertRaises(ValueError):
                profiles.profile_reminder(bad)

    def test_profile_guidance_has_no_tool_or_permission_claim(self):
        for profile in profiles.PROFILE_NAMES:
            text = profiles.profile_reminder(profile)
            self.assertTrue("does not" in text)
            for claimed_name in ("exec_command", "update_plan", "list_connectors", "mcp__", "run_officejs"):
                self.assertNotIn(claimed_name, text)
        self.assertIn("source notation", profiles.profile_reminder("study_notes"))
        self.assertIn("actual test results", profiles.profile_reminder("software_development"))
        self.assertIn("render and inspect", profiles.profile_reminder("document_creation"))


class PolicyTests(TestCase):
    def test_v1_unversioned_and_explicit_compatibility(self):
        for data in (policy_v1(), {**policy_v1(), "version": 1}):
            result = guard._parse_policy(encode(data))
            self.assertEqual(result["version"], 1)
            self.assertEqual(result["profile_by_session"], {})
            self.assertEqual(result["session_ids"], frozenset(data["session_ids"]))

    def test_v2_map_optional_and_generic_explicit(self):
        for additions in ({}, {"profile_by_session": {}}, {"profile_by_session": {ROOT: "generic"}}):
            data = {**policy_v1(), "version": 2, **additions}
            parsed = guard._parse_policy(encode(data))
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["profile_by_session"], additions.get("profile_by_session", {}))

    def test_checked_in_policy_is_disabled_by_default(self):
        data = json.loads(Path(guard.__file__).with_name("tool_contract_policy.json").read_text(encoding="utf-8"))
        self.assertFalse(data["enabled"])
        self.assertEqual(data.get("profile_by_session", {}), {})
        self.assertIsNone(guard._parse_policy(encode(data)))

    def test_document_profile_requires_explicit_mapping(self):
        data = policy_v2(); data["profile_by_session"] = {ROOT: "document_creation"}
        self.assertEqual(guard._parse_policy(encode(data))["profile_by_session"], {ROOT: "document_creation"})

    def test_unknown_version_fields_profiles_and_sessions_fail_closed(self):
        patches = [{"version": v} for v in (0, 3, True, "2", 2.0, None)]
        patches += [{"unknown": True}, {"fallback_profile": "study_notes"}, {"profile_by_session": {ROOT: "automatic"}}, {"profile_by_session": {"unconfigured": "generic"}}, {"profile_by_session": {ROOT: None}}, {"profile_by_session": {ROOT: []}}, {"profile_by_session": []}]
        for patch in patches:
            data = policy_v2(); data.update(patch)
            with self.subTest(patch=patch): self.assertIsNone(guard._parse_policy(encode(data)))
        for data in ({**policy_v1(), "profile_by_session": {}}, {**policy_v1(), "version": 1, "profile_by_session": {ROOT: "study_notes"}}):
            self.assertIsNone(guard._parse_policy(encode(data)))

    def test_malformed_disabled_duplicate_and_oversized_policies(self):
        invalid = [b"bad", b"[]", b'{"enabled":true,"enabled":false}', b"x" * (guard.MAX_POLICY_BYTES + 1)]
        for patch in ({"enabled": False}, {"enabled": 1}, {"scope": "all"}, {"models": []}, {"models": [MODEL, MODEL]}, {"models": [False]}, {"session_ids": []}, {"session_ids": ["x\n"]}):
            data = policy_v2(); data.update(patch); invalid.append(encode(data))
        for data in invalid:
            self.assertIsNone(guard._parse_policy(data))
        data = encode(policy_v2()).replace(b'"software_development"', b'"software_development","'+ROOT.encode()+b'":"study_notes"', 1)
        self.assertIsNone(guard._parse_policy(data))

    def test_missing_file_and_mtime_cache(self):
        path = mock.MagicMock(); path.__str__.return_value = "test-task-policy"
        path.stat.side_effect = FileNotFoundError
        with mock.patch.object(guard, "POLICY_PATH", path): self.assertIsNone(guard._load_policy())
        path.stat.side_effect = None
        path.stat.return_value = mock.Mock(st_mtime_ns=1, st_size=100)
        handle = path.open.return_value.__enter__.return_value
        handle.read.return_value = encode(policy_v2())
        with mock.patch.object(guard, "POLICY_PATH", path):
            self.assertIsNotNone(guard._load_policy()); self.assertIsNotNone(guard._load_policy())
            self.assertEqual(path.open.call_count, 1)
            path.stat.return_value = mock.Mock(st_mtime_ns=2, st_size=100)
            handle.read.return_value = b"invalid"
            self.assertIsNone(guard._load_policy())
            self.assertEqual(path.open.call_count, 2)


class GuardTests(TestCase):
    def setUp(self):
        patcher = mock.patch.object(guard, "_load_policy", return_value=guard._parse_policy(encode(policy_v2())))
        patcher.start(); self.addCleanup(patcher.stop)

    def apply(self, doc=None, raw=None, h=None, method="POST", path="/v1/responses", limit=1000000):
        original = raw if raw is not None else encode(document() if doc is None else doc)
        diagnostics = {"secret": "must be removed"}
        result, count = guard.add_catalog_guard(original, h if h is not None else headers(), method, path, max_body_bytes=limit, diagnostics=diagnostics)
        self.assertEqual(set(diagnostics), {"reason", "catalog_count", "catalog_hash", "task_profile"})
        self.assertIn(diagnostics["task_profile"], profiles.PROFILE_NAMES)
        return original, result, count, diagnostics

    def test_v3_complete_inventory_and_preserves_every_original_field(self):
        doc = document(); doc["metadata"] = {"nonsecret": "original"}
        raw, result, count, diagnostics = self.apply(doc)
        self.assertEqual(count, 1); self.assertEqual(diagnostics["reason"], "guard_added")
        changed = json.loads(result)
        self.assertEqual({**changed, "input": changed["input"][:-1]}, doc)
        text = reminder(result)
        self.assertTrue(text.startswith(guard.GUARD_MARKER + "\n"))
        self.assertEqual(json.loads(text.split(CATALOG_LABEL, 1)[1]), [{"name": "probe", "type": "function"}])
        self.assertIn("update_plan is not declared", text)
        self.assertIn("list_connectors is not declared", text)
        self.assertIn("describe the steps in ordinary text", text)
        self.assertEqual(diagnostics["task_profile"], "software_development")
        self.assertLessEqual(len(text.encode("utf-8")), 32768)

    def test_profile_is_explicit_by_session_not_guessed_from_task(self):
        for session, expected in ((ROOT, "software_development"), (DEPLOY, "software_development"), (PROBABILITY, "study_notes")):
            _, result, count, diagnostics = self.apply(h=headers(session))
            self.assertEqual(count, 1); self.assertEqual(diagnostics["task_profile"], expected)
            self.assertIn("Task profile: " + expected, reminder(result))
        data = policy_v2(); data["profile_by_session"] = {}
        doc = document(); doc["input"][0]["content"][0]["text"] = "Write software, study notes and a document."
        with mock.patch.object(guard, "_load_policy", return_value=guard._parse_policy(encode(data))):
            _, result, count, diagnostics = self.apply(doc)
        self.assertEqual(count, 1); self.assertEqual(diagnostics["task_profile"], "generic")
        self.assertIn("Task profile: generic", reminder(result))

    def test_legacy_policy_defaults_generic(self):
        for policy in (guard._parse_policy(encode(policy_v1())), {"scope": "session_ids", "session_ids": frozenset({ROOT}), "models": frozenset({MODEL})}):
            with mock.patch.object(guard, "_load_policy", return_value=policy):
                _, result, count, diag = self.apply()
            self.assertEqual(count, 1); self.assertEqual(diag["task_profile"], "generic")

    def test_all_bps_unmapped_session_is_generic_and_still_model_scoped(self):
        data = policy_v2(); data["scope"] = "all_bps"
        with mock.patch.object(guard, "_load_policy", return_value=guard._parse_policy(encode(data))):
            _, _, count, diag = self.apply(h=headers("unmapped"))
            self.assertEqual(count, 1); self.assertEqual(diag["task_profile"], "generic")
            doc = document(); doc["model"] = "another-model"
            raw, result, count, diag = self.apply(doc, h=headers("unmapped"))
            self.assertEqual((result, count, diag["reason"]), (raw, 0, "outside_model_scope"))

    def test_full_qualified_name_inventory_and_declared_special_tools(self):
        doc = document()
        doc["tools"] += [{"type": "namespace", "name": "ns", "tools": [function("update_plan"), {"type": "custom", "name": "list_connectors"}]}, function("update_plan")]
        doc["input"].append({"type": "additional_tools", "tools": [{"type": "namespace", "name": "other", "tools": [function("list_connectors"), function("update_plan")]}]})
        _, result, count, diag = self.apply(doc)
        self.assertEqual(count, 1)
        text = reminder(result)
        expected = [{"name": item["name"], "type": item["type"]} for item in collect_tool_contracts(doc).contracts]
        self.assertEqual(json.loads(text.split(CATALOG_LABEL, 1)[1]), expected)
        self.assertIn('update_plan is declared at these exact qualified names: ["ns.update_plan","other.update_plan","update_plan"]', text)
        self.assertIn('list_connectors is declared at these exact qualified names: ["ns.list_connectors","other.list_connectors"]', text)
        self.assertNotIn("describe the steps in ordinary text", text)
        self.assertEqual(diag["catalog_count"], len(expected))

    def test_nearby_names_do_not_create_aliases(self):
        doc = document(); doc["tools"] = [function("update_plan_helper"), function("xlist_connectors"), function("plan")]
        _, result, count, _ = self.apply(doc)
        self.assertEqual(count, 1)
        self.assertIn("update_plan is not declared", reminder(result))
        self.assertIn("list_connectors is not declared", reminder(result))

    def test_catalog_json_escaping_unicode_and_custom_type(self):
        doc = document(); doc["tools"] = [function('中文\\"tool'), {"type": "custom", "name": "edit", "format": {"type": "text"}}]
        _, result, count, _ = self.apply(doc)
        self.assertEqual(count, 1)
        self.assertEqual(json.loads(reminder(result).split(CATALOG_LABEL, 1)[1]), [{"name": "edit", "type": "custom"}, {"name": '中文\\"tool', "type": "function"}])

    def test_no_transport_inference_and_four_carrier_rules_preserved(self):
        doc = document(); doc["tools"] = [function("exec_like", {"properties": {"cmd": {"type": "string"}}}), function("code_like", {"oneOf": [{"properties": {"code": {"type": "string"}}}, {"properties": {"completion_summary": {"type": "string"}}}]})]
        _, result, count, _ = self.apply(doc)
        self.assertEqual(count, 1); text = reminder(result)
        for token in ("FUNCTION:", "FUNCTION_CODE:", "FUNCTION_CMD:", "CUSTOM:", "codex2api.function_code/CATALOG_NAME", "codex2api.function_cmd/CATALOG_NAME", "codex2api.custom/CATALOG_NAME", "exact raw input", "destructive=false and references=[]", "actual call/result pairing", "does not promise"):
            self.assertIn(token, text)
        self.assertTrue(all(set(item) == {"name", "type"} and item["type"] == "function" for item in json.loads(text.split(CATALOG_LABEL, 1)[1])))

    def test_hosted_native_is_not_in_client_inventory(self):
        doc = document(); doc["tools"].insert(0, {"type": "web_search"})
        _, result, count, diag = self.apply(doc)
        self.assertEqual(count, 1); self.assertEqual(diag["catalog_count"], 1)
        text = reminder(result)
        self.assertNotIn("web_search", text.split(CATALOG_LABEL, 1)[1])
        self.assertIn("native tool declarations", text)
        self.assertIn("empty tool shells", text)
        self.assertIn("never report fake success", text)
        self.assertIn("Keep actual errors visible", text)

    def test_budget_exact_boundary_and_one_byte_over(self):
        _, result, count, _ = self.apply()
        size = len(reminder(result).encode("utf-8"))
        with mock.patch.object(guard, "MAX_GUARD_TEXT_BYTES", size): self.assertEqual(self.apply()[2], 1)
        with mock.patch.object(guard, "MAX_GUARD_TEXT_BYTES", size - 1):
            raw, out, count, diag = self.apply()
        self.assertEqual((out, count, diag["reason"]), (raw, 0, "guard_text_size_limit"))

    def test_large_complete_catalog_never_truncated(self):
        doc = document(); doc["tools"] = [function(str(i).zfill(3) + "x" * 235) for i in range(140)]
        raw, out, count, diag = self.apply(doc)
        self.assertEqual((out, count, diag["reason"]), (raw, 0, "guard_text_size_limit"))
        self.assertEqual(diag["catalog_count"], 140)

    def test_budget_counts_utf8_bytes_not_characters(self):
        doc = document(); doc["tools"] = [function(str(i).zfill(3) + "中" * 70) for i in range(130)]
        summary = collect_tool_contracts(doc)
        with mock.patch.object(guard, "MAX_GUARD_TEXT_BYTES", 100000):
            text = guard._guard_item(summary, "software_development")["content"][0]["text"]
        self.assertLess(len(text), 32768); self.assertGreater(len(text.encode("utf-8")), 32768)
        raw, out, count, diag = self.apply(doc)
        self.assertEqual((out, count, diag["reason"]), (raw, 0, "guard_text_size_limit"))

    def test_v1_v2_v3_historical_guards_are_untouched(self):
        for marker, expected in ((guard.LEGACY_GUARD_MARKER, "legacy_guard_present"), (guard.PREVIOUS_GUARD_MARKER, "guard_present"), (guard.GUARD_MARKER, "guard_present")):
            doc = document(); doc["input"].append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": marker + "\nold catalog hash; preserve exact history"}]})
            raw, out, count, diag = self.apply(doc)
            self.assertEqual((out, count, diag["reason"]), (raw, 0, expected))
        _, first, count, _ = self.apply()
        self.assertEqual(count, 1)
        _, second, count, diag = self.apply(raw=first)
        self.assertEqual((second, count, diag["reason"]), (first, 0, "guard_present"))

    def test_user_quoted_guard_is_not_developer_guard(self):
        doc = document(); doc["input"][0]["content"][0]["text"] = guard.PREVIOUS_GUARD_MARKER + "\nquoted text"
        self.assertEqual(self.apply(doc)[2], 1)

    def test_call_result_pairing_and_identifiers_are_not_rewritten(self):
        doc = document(); doc["input"] += [{"type": "function_call", "name": "probe", "call_id": "real", "arguments": '{"value":"x"}'}, {"type": "function_call_output", "call_id": "real", "output": "actual"}]
        _, result, count, _ = self.apply(doc)
        self.assertEqual(count, 1); self.assertEqual(json.loads(result)["input"][:-1], doc["input"])

    def test_explicit_auto_allowed_other_tool_choices_preserved(self):
        doc = document(); doc["tool_choice"] = "auto"
        self.assertEqual(self.apply(doc)[2], 1)
        for choice in ("none", "required", None, False, {"type": "function", "name": "probe"}):
            doc["tool_choice"] = choice
            raw, out, count, diag = self.apply(doc)
            self.assertEqual((out, count, diag["reason"]), (raw, 0, "tool_choice_not_auto"))

    def test_scope_method_model_and_content_type(self):
        for options in ({"h": headers("other")}, {"method": "GET"}, {"path": "/v1/responses/compact"}, {"h": headers(**{"Content-Type": "text/plain"})}):
            raw, out, count, _ = self.apply(**options); self.assertEqual((out, count), (raw, 0))
        for model in (None, [], "other"):
            doc = document(); doc["model"] = model
            raw, out, count, diag = self.apply(doc)
            self.assertEqual((out, count, diag["reason"]), (raw, 0, "outside_model_scope"))

    def test_disabled_policy_is_noop_with_safe_diagnostics(self):
        with mock.patch.object(guard, "_load_policy", return_value=None):
            raw, out, count, diag = self.apply()
        self.assertEqual((out, count, diag["reason"]), (raw, 0, "policy_disabled"))
        self.assertEqual(diag["task_profile"], "generic")

    def test_duplicate_and_integrity_headers_preserve_body(self):
        for name in context.INTEGRITY_HEADERS:
            raw, out, count, diag = self.apply(h=headers(**{name: "present"}))
            self.assertEqual((out, count, diag["reason"]), (raw, 0, "integrity_protected"))
        for name in ("Session-Id", "Content-Type", "Content-Encoding"):
            h = headers()
            if name == "Content-Encoding": h.add(name, "identity")
            h.add(name, h.get(name, "identity"))
            raw, out, count, diag = self.apply(h=h)
            self.assertEqual((out, count, diag["reason"]), (raw, 0, "ambiguous_headers"))

    def test_gzip_zstd_bounded_trailing_and_unknown_encoding(self):
        raw = encode(document())
        for encoding, packed in (("gzip", gzip.compress(raw)), ("zstd", zstd.compress(raw))):
            _, out, count, _ = self.apply(raw=packed, h=headers(**{"Content-Encoding": encoding}))
            self.assertEqual(count, 1); self.assertEqual(json.loads(out)["tools"], document()["tools"])
            _, out, count, _ = self.apply(raw=packed, h=headers(**{"Content-Encoding": encoding}), limit=len(raw)-1)
            self.assertEqual((out, count), (packed, 0))
            _, out, count, _ = self.apply(raw=packed+b"trailing", h=headers(**{"Content-Encoding": encoding}))
            self.assertEqual((out, count), (packed+b"trailing", 0))
        _, out, count, _ = self.apply(raw=raw, h=headers(**{"Content-Encoding": "br"}))
        self.assertEqual((out, count), (raw, 0))

    def test_json_uniqueness_precision_and_output_size_protection(self):
        valid = encode(document())
        cases = [b"{bad", b'{"input":[],"input":[]}', valid[:-1]+b',"n":NaN}', valid[:-1]+b',"n":1e309}', valid[:-1]+b',"n":0.123456789012345678901}', b"[]", b'{}', b"\xff"]
        for raw in cases:
            _, out, count, _ = self.apply(raw=raw); self.assertEqual((out, count), (raw, 0))
        raw, out, count, diag = self.apply(raw=valid, limit=len(valid)+1)
        self.assertEqual((out, count, diag["reason"]), (raw, 0, "output_size_limit"))

    def test_conflicts_and_unknown_catalog_do_not_leak_or_guess(self):
        for extra in (function(schema={"const": "private-value"}), {"type": "unknown_hosted"}):
            doc = document(); doc["tools"].append(extra)
            raw, out, count, diag = self.apply(doc)
            self.assertEqual((out, count), (raw, 0))
            self.assertNotIn("private-value", json.dumps(diag))
            self.assertNotIn("probe", json.dumps(diag))


if __name__ == "__main__":
    main()
