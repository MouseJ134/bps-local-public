"""Localhost-only schema guard and compression-chain integration."""
import gzip
import json
import unittest
from compression import zstd
import catalog_compat
import test_bridge_integration as integration
from test_schema_contract_compat import fixture, policy
from tool_contracts import collect_tool_contracts


class ParameterGuardBridgeTests(unittest.IsolatedAsyncioTestCase):
    CLIENT_TOKEN = integration.BridgeIntegrationTests.CLIENT_TOKEN
    UPSTREAM_TOKEN = integration.BridgeIntegrationTests.UPSTREAM_TOKEN
    SSE_BYTES = integration.BridgeIntegrationTests.SSE_BYTES
    asyncSetUp = integration.BridgeIntegrationTests.asyncSetUp
    _cleanup = integration.BridgeIntegrationTests._cleanup
    _socket = integration.BridgeIntegrationTests._socket
    _start_server = integration.BridgeIntegrationTests._start_server
    write_policy = integration.BridgeIntegrationTests.write_policy
    headers = integration.BridgeIntegrationTests.headers
    request = integration.BridgeIntegrationTests.request

    async def exercise(self, encoding, old_v3):
        self.policy = policy(); self.write_policy()
        doc = fixture()
        if old_v3:
            doc["input"].append(catalog_compat._guard_item(collect_tool_contracts(doc)))
        raw = json.dumps(doc).encode(); h = self.headers(); h["Content-Encoding"] = encoding
        raw = gzip.compress(raw) if encoding == "gzip" else zstd.compress(raw)
        status, _, reply = await self.request(raw=raw, headers=h)
        self.assertEqual((status, reply), (200, self.upstream_body))
        self.assertEqual(len(self.observed), 1)
        observed = self.observed[0]; out = json.loads(observed["body"])
        self.assertEqual(out["input"][:len(doc["input"])], doc["input"])
        self.assertEqual(out["tools"], doc["tools"])
        self.assertEqual(len(out["input"])-len(doc["input"]), 1 if old_v3 else 2)
        self.assertNotIn("Content-Encoding", observed["headers"])
        self.assertEqual(int(observed["headers"]["Content-Length"]), len(observed["body"]))
        self.assertIs(self.events[-1]["parameter_guard_added"], True)
        self.assertEqual(self.events[-1]["catalog_guard_added"], 0 if old_v3 else 1)

    async def test_existing_v3_gzip_parameter_hook(self):
        await self.exercise("gzip", True)

    async def test_existing_v3_zstd_parameter_hook(self):
        await self.exercise("zstd", True)

    async def test_new_catalog_then_parameter_hook_gzip(self):
        await self.exercise("gzip", False)

    async def test_new_catalog_then_parameter_hook_zstd(self):
        await self.exercise("zstd", False)

    async def test_disabled_policy_keeps_compressed_request(self):
        self.policy = policy(); self.policy["enabled"] = False; self.write_policy()
        h = self.headers(); h["Content-Encoding"] = "gzip"; raw = gzip.compress(json.dumps(fixture()).encode())
        status, _, _ = await self.request(raw=raw, headers=h)
        self.assertEqual(status, 200); self.assertEqual(self.observed[0]["body"], raw)
        self.assertEqual(self.observed[0]["headers"]["Content-Encoding"], "gzip")
        self.assertFalse(self.events[-1]["parameter_guard_added"])

    async def test_error_observer_still_reports_schema_error(self):
        self.upstream_body = b'event: response.failed\ndata: {"type":"response.failed","response":{"error":{"code":"basispoints_protocol_error","message":"basispoints function arguments do not satisfy the declared tool schema or validation resource limits"}}}\n\n'
        await self.exercise("gzip", True)
        self.assertEqual(self.events[-1]["upstream_error_diagnostics"]["errors"][0]["category"], "schema_or_resource")
