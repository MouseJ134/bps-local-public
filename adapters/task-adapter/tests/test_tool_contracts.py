"""Offline tests; no network, credentials, tools or production mutations."""
from __future__ import annotations
import copy
import gzip
import hashlib
import json
from pathlib import Path
from unittest import TestCase, main, mock
from compression import zstd
from multidict import CIMultiDict
import catalog_compat as guard
import request_codec as context
import tool_contracts as contracts

SESSION = "synthetic-session-dev"
POLICY = {"scope": "session_ids", "session_ids": frozenset({SESSION}), "models": frozenset({"synthetic-bps-model"})}


def function(name="probe", schema=None, **kw):
    return {"type": "function", "name": name, "parameters": schema if schema is not None else {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}, **kw}


def document():
    return {"model": "synthetic-bps-model", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "a user task"}]}], "tools": [function()]}


def encode(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()


def headers(**kw):
    h = CIMultiDict({"Session-Id": SESSION, "Content-Type": "application/json"})
    h.update(kw)
    return h


class ContractTests(TestCase):
    def test_merge_qualified_root_custom_and_additional(self):
        doc = document()
        doc["tools"].append({"type": "namespace", "name": "ns", "tools": [function("read"), {"type": "custom", "name": "patch", "format": {"type": "text"}}]})
        doc["input"].append({"type": "additional_tools", "tools": [function("probe", description="other prose"), function("later")]})
        s = contracts.collect_tool_contracts(doc)
        self.assertEqual(s.count, 4)
        self.assertEqual([(t["name"], t["type"]) for t in s.contracts], [("later", "function"), ("ns.patch", "custom"), ("ns.read", "function"), ("probe", "function")])
        self.assertEqual(len(s.digest), 64)

    def test_additional_only_catalog(self):
        doc = document(); del doc["tools"]
        doc["input"].append({"type": "additional_tools", "tools": [function()]})
        self.assertEqual(contracts.collect_tool_contracts(doc).count, 1)

    def test_hash_stable_under_order_and_schema_annotations(self):
        a = document(); a["tools"].append(function("other"))
        b = copy.deepcopy(a); b["tools"].reverse()
        b["tools"][1]["parameters"]["description"] = "schema prose"
        b["tools"][1]["parameters"]["properties"]["x"]["title"] = "prose"
        self.assertEqual(contracts.collect_tool_contracts(a).digest, contracts.collect_tool_contracts(b).digest)

    def test_real_parameter_names_and_literals_survive(self):
        literal = {"description": "real", "title": "value", "default": {"description": "nested"}}
        schema = {"type": "object", "description": "annotation", "title": "annotation", "$comment": "annotation", "properties": {k: {"type": "string", "description": "annotation"} for k in ("description", "title", "default")}, "$defs": {"description": {"type": "string", "description": "annotation"}}, "patternProperties": {"title": {"type": "string"}}, "enum": [literal], "const": literal, "default": literal, "x-literal": literal}
        got = contracts.canonicalize_schema(schema)
        self.assertNotIn("description", got)
        self.assertEqual(set(got["properties"]), {"description", "title", "default"})
        self.assertIn("description", got["$defs"])
        for key in ("const", "default", "x-literal"):
            self.assertEqual(got[key], literal)
        self.assertEqual(got["enum"], [literal])
        self.assertEqual(schema["description"], "annotation")

    def test_literal_changes_are_conflicts(self):
        for key in ("enum", "const", "default"):
            with self.subTest(key=key):
                one = function(schema={key: [{"description": "one"}] if key == "enum" else {"description": "one"}})
                two = copy.deepcopy(one)
                two["parameters"][key] = [{"description": "two"}] if key == "enum" else {"description": "two"}
                doc = document(); doc["tools"] = [one, two]
                with self.assertRaisesRegex(contracts.CatalogError, "conflicting_catalog"):
                    contracts.collect_tool_contracts(doc)

    def test_real_parameter_name_change_is_conflict(self):
        doc = document()
        a = function(schema={"type": "object", "properties": {"description": {"type": "string"}}})
        b = function(schema={"type": "object", "properties": {"title": {"type": "string"}}})
        doc["tools"] = [a,b]
        with self.assertRaisesRegex(contracts.CatalogError, "conflicting_catalog"):
            contracts.collect_tool_contracts(doc)

    def test_function_custom_collision(self):
        doc = document(); doc["tools"].append({"type": "custom", "name": "probe"})
        with self.assertRaisesRegex(contracts.CatalogError, "conflicting_catalog"):
            contracts.collect_tool_contracts(doc)

    def test_nested_additional_is_not_guessed(self):
        for item in ({"type": "message", "additional_tools": [function()]}, {"type": "message", "metadata": {"x": {"type": "additional_tools", "tools": [function()]}}}):
            doc = document(); doc["input"].append(item)
            with self.assertRaisesRegex(contracts.CatalogError, "unsupported_additional_tools"):
                contracts.collect_tool_contracts(doc)

    def test_root_additional_is_unsupported(self):
        doc = document(); doc["additional_tools"] = [function()]
        with self.assertRaisesRegex(contracts.CatalogError, "unsupported_additional_tools"):
            contracts.collect_tool_contracts(doc)

    def test_schema_parameter_additional_tools_is_not_history(self):
        doc = document(); doc["tools"] = [function(schema={"properties": {"additional_tools": {"type": "string"}}})]
        self.assertEqual(contracts.collect_tool_contracts(doc).count, 1)

    def test_unknown_kind_or_bad_catalog_skips(self):
        for tools in (None, {}, [{"type": "computer_use", "name": "c"}], [{"type": "function", "name": ""}], [{"type": "function", "name": "x\n"}], [{"type": "namespace", "name": "ns", "tools": None}]):
            with self.subTest(tools=tools):
                doc = document(); doc["tools"] = tools
                with self.assertRaises(contracts.CatalogError): contracts.collect_tool_contracts(doc)

    def test_tool_depth_and_schema_limits(self):
        with mock.patch.object(contracts, "MAX_TOOLS", 1):
            doc=document(); doc["tools"].append(function("two"))
            with self.assertRaisesRegex(contracts.CatalogError, "catalog_tool_limit"): contracts.collect_tool_contracts(doc)
        with mock.patch.object(contracts, "MAX_TOOL_DECLARATIONS", 1):
            doc=document(); doc["tools"].append(function())
            with self.assertRaisesRegex(contracts.CatalogError, "catalog_tool_limit"): contracts.collect_tool_contracts(doc)
        with mock.patch.object(contracts, "MAX_SCHEMA_BYTES", 10):
            with self.assertRaisesRegex(contracts.CatalogError, "catalog_schema_limit"): contracts.collect_tool_contracts(document())
        with mock.patch.object(contracts, "MAX_VALUE_DEPTH", 2):
            doc=document();doc["input"]=[]
            with self.assertRaisesRegex(contracts.CatalogError, "catalog_structure_limit"): contracts.collect_tool_contracts(doc)
        with mock.patch.object(contracts, "MAX_NAMESPACE_DEPTH", 0):
            doc=document(); doc["tools"]=[{"type":"namespace","name":"ns","tools":[function()]}]
            with self.assertRaisesRegex(contracts.CatalogError, "catalog_structure_limit"): contracts.collect_tool_contracts(doc)

    def test_known_root_hosted_tools_are_metadata_only(self):
        doc=document()
        doc["tools"]=[{"type":"web_search","search_context_size":"medium"},{"type":"namespace","name":"functions","tools":[function("exec_command"),{"type":"custom","name":"apply_patch","format":{"type":"text"}}]}]
        summary=contracts.collect_tool_contracts(doc)
        self.assertEqual(summary.count,2)
        self.assertEqual(summary.hosted_types,("web_search",))
        self.assertEqual([x["name"] for x in summary.contracts],["functions.apply_patch","functions.exec_command"])
        doc["tools"].append({"type":"web_search_preview"})
        self.assertEqual(contracts.collect_tool_contracts(doc).hosted_types,("web_search","web_search_preview"))
        self.assertEqual(contracts.collect_tool_contracts(doc).digest,summary.digest)

    def test_hosted_inside_namespace_or_unknown_hosted_skips(self):
        for declaration in ({"type":"namespace","name":"ns","tools":[{"type":"web_search"}]},{"type":"new_hosted_tool"}):
            doc=document();doc["tools"].append(declaration)
            with self.assertRaisesRegex(contracts.CatalogError,"unsupported_catalog_kind"):
                contracts.collect_tool_contracts(doc)

    def test_namespace_and_leaf_identity_collision_both_orders(self):
        namespace={"type":"namespace","name":"foo","tools":[function("bar")]}
        for tools in ([function("foo"),namespace],[namespace,function("foo")]):
            doc=document();doc["tools"]=tools
            with self.assertRaisesRegex(contracts.CatalogError,"conflicting_catalog_identity"):
                contracts.collect_tool_contracts(doc)

    def test_same_namespace_can_merge_without_aliases(self):
        doc=document();doc["tools"]=[{"type":"namespace","name":"foo","tools":[function("bar")]},{"type":"namespace","name":"foo","tools":[function("baz")]}]
        self.assertEqual([x["name"] for x in contracts.collect_tool_contracts(doc).contracts],["foo.bar","foo.baz"])

    def test_schema_aliases_are_not_silently_discarded(self):
        for alias in ("inputSchema","input_schema"):
            for include_parameters in (False,True):
                doc=document();tool=function();tool[alias]={"type":"object","required":["secret_required"]}
                if not include_parameters:del tool["parameters"]
                doc["tools"]=[tool]
                with self.assertRaisesRegex(contracts.CatalogError,"unsupported_schema_alias"):
                    contracts.collect_tool_contracts(doc)

    def test_dotted_leaf_and_namespace_identity_collision(self):
        namespace={"type":"namespace","name":"a","tools":[function("b")]}
        for tools in ([function("a.b"),namespace],[namespace,function("a.b")]):
            doc=document();doc["tools"]=tools
            with self.assertRaisesRegex(contracts.CatalogError,"conflicting_catalog_identity"):
                contracts.collect_tool_contracts(doc)

    def test_dotted_namespace_and_nested_identity_collision(self):
        a={"type":"namespace","name":"a.b","tools":[function("c")]}
        b={"type":"namespace","name":"a","tools":[{"type":"namespace","name":"b","tools":[function("c")]}]}
        for tools in ([a,b],[b,a]):
            doc=document();doc["tools"]=tools
            with self.assertRaisesRegex(contracts.CatalogError,"conflicting_catalog_identity"):
                contracts.collect_tool_contracts(doc)

    def test_optional_code_and_oneof_preserve_contract(self):
        schema={"oneOf":[{"type":"object","properties":{"code":{"type":"string"}},"required":["code"]},{"type":"object","properties":{"completion_summary":{"type":"string"}},"required":["completion_summary"]}]}
        doc=document(); doc["tools"]=[function("cua",schema=schema), function("optional",schema={"properties":{"code":{"type":"string"}}})]
        got=contracts.collect_tool_contracts(doc)
        self.assertEqual(got.contracts[0]["parameters"],schema)
        self.assertTrue(all("transport" not in c for c in got.contracts))
