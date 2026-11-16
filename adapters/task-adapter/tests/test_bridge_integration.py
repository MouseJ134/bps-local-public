"""Localhost HTTP/SSE integration; only synthetic temporary credentials."""
import asyncio
from compression import zstd
import gzip
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock
from aiohttp import ClientSession, ClientTimeout, web
import auth_bridge

class BridgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    CLIENT_TOKEN = "synthetic-client-token-not-a-real-credential"
    UPSTREAM_TOKEN = "synthetic-upstream-token-not-a-real-credential"
    SSE_BYTES = (
        b'event: response.created\ndata: {"type":"response.created"}\n\n'
        + 'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"合成"}\n\n'.encode()
        + b'event: response.completed\ndata: {"type":"response.completed"}\n\ndata: [DONE]\n\n'
    )

    async def asyncSetUp(self):
        self.observed, self.events, self.runners, self.sockets = [], [], [], []
        self.client = None
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.emit_patch = mock.patch("auth_bridge.emit", side_effect=self.events.append)
        self.emit_patch.start()
        root = Path(self.temp.name)
        self.client_file = root / "client.json"
        self.upstream_file = root / "upstream-key"
        self.policy_file = root / "policy.json"
        self.client_file.write_text(json.dumps({"token": self.CLIENT_TOKEN}), encoding="utf-8")
        self.upstream_file.write_text(self.UPSTREAM_TOKEN, encoding="utf-8")
        self.policy = {"enabled": True, "version": 2, "scope": "session_ids", "session_ids": ["synthetic-session"], "models": ["synthetic-model"], "profile_by_session": {"synthetic-session": "software_development"}}
        self.write_policy()
        self.upstream_status = 200
        self.upstream_headers = {"Content-Type": "text/event-stream; charset=utf-8", "X-Synthetic-Upstream": "yes"}
        self.upstream_body = self.SSE_BYTES
        async def upstream_handler(request):
            self.observed.append({"method": request.method, "path_qs": request.path_qs, "headers": request.headers.copy(), "body": await request.read()})
            response = web.StreamResponse(status=self.upstream_status, headers=self.upstream_headers)
            await response.prepare(request)
            for start in range(0, len(self.upstream_body), 17):
                await response.write(self.upstream_body[start:start+17])
                await asyncio.sleep(0)
            await response.write_eof()
            return response
        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{tail:.*}", upstream_handler)
        upstream_port = await self._start_server(upstream_app)
        bridge_socket = self._socket()
        bridge_port = bridge_socket.getsockname()[1]
        self.settings = auth_bridge.Settings(listen_port=bridge_port, client_auth_file=self.client_file, upstream_key_file=self.upstream_file, idle_timeout=3.0, upstream_base=f"http://127.0.0.1:{upstream_port}", policy_file=self.policy_file)
        await self._start_server(auth_bridge.create_app(self.settings), bridge_socket)
        self.base = f"http://127.0.0.1:{bridge_port}"
        self.client = ClientSession(timeout=ClientTimeout(total=8), trust_env=False, auto_decompress=False, skip_auto_headers={"Accept-Encoding", "User-Agent"})

    def write_policy(self):
        self.policy_file.write_text(json.dumps(self.policy), encoding="utf-8")

    def _socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0)); sock.setblocking(False)
        self.sockets.append(sock)
        return sock

    async def _start_server(self, app, sock=None):
        sock = sock or self._socket()
        runner = web.AppRunner(app, access_log=None, auto_decompress=False, handler_cancellation=True)
        self.runners.append(runner)
        await runner.setup()
        await web.SockSite(runner, sock).start()
        return sock.getsockname()[1]

    async def _cleanup(self):
        if self.client is not None: await self.client.close()
        for runner in reversed(self.runners): await runner.cleanup()
        for sock in self.sockets: sock.close()
        if hasattr(self, "emit_patch"): self.emit_patch.stop()
        self.temp.cleanup()

    def document(self):
        return {"model": "synthetic-model", "stream": True, "input": [
            {"role": "user", "content": "Synthetic request."},
            {"type": "function_call", "name": "probe", "call_id": "synthetic-call", "arguments": '{"value":"ok"}'},
            {"type": "function_call_output", "call_id": "synthetic-call", "output": "synthetic result"}],
            "tools": [{"type": "function", "name": "probe", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}]}

    def headers(self):
        return {"Authorization": "Bearer " + self.CLIENT_TOKEN, "Content-Type": "application/json", "Session-Id": "synthetic-session", "ChatGPT-Account-Id": "synthetic-private-account", "X-API-Key": "synthetic-alternate-key", "Cookie": "synthetic=not-real"}

    async def request(self, document=None, *, raw=None, headers=None, method="POST", path="/v1/responses"):
        if raw is None: raw = json.dumps(document or self.document(), ensure_ascii=False, indent=2).encode()
        async with self.client.request(method, self.base+path, data=raw, headers=headers or self.headers(), allow_redirects=False) as response:
            result = response.status, response.headers.copy(), await response.read()
        await asyncio.sleep(0)
        return result

    def error_stream(self, message):
        self.upstream_body = ("event: response.failed\ndata: " + json.dumps({"type": "response.failed", "response": {"error": {"code": "basispoints_protocol_error", "message": message}}}) + "\n\n").encode()

    async def test_guard_append_preserves_tools_pairs_and_sse(self):
        doc=self.document()
        status,headers,body=await self.request(doc)
        self.assertEqual((status,body),(200,self.SSE_BYTES))
        changed=json.loads(self.observed[0]["body"])
        self.assertEqual({**changed,"input":changed["input"][:-1]},doc)
        self.assertEqual(self.events[-1]["catalog_guard_added"],1)
        self.assertEqual(headers["X-Synthetic-Upstream"],"yes")

    async def test_eligible_compressed_body_is_decoded_once(self):
        for encoding,compress in (("zstd",zstd.compress),("gzip",gzip.compress)):
            h=self.headers();h["Content-Encoding"]=encoding
            raw=compress(json.dumps(self.document()).encode())
            status,_,body=await self.request(raw=raw,headers=h)
            self.assertEqual((status,body),(200,self.SSE_BYTES))
            self.assertNotIn("Content-Encoding",self.observed[-1]["headers"])
            self.assertEqual(json.loads(self.observed[-1]["body"])["input"][:-1],self.document()["input"])
        self.assertEqual(len(self.observed),2)

    async def test_disabled_policy_preserves_compressed_body(self):
        self.policy["enabled"]=False;self.write_policy()
        h=self.headers();h["Content-Encoding"]="zstd"
        raw=zstd.compress(json.dumps(self.document(),indent=2).encode())
        status,_,body=await self.request(raw=raw,headers=h)
        self.assertEqual((status,body),(200,self.SSE_BYTES))
        self.assertEqual(self.observed[0]["body"],raw)
        self.assertEqual(self.observed[0]["headers"]["Content-Encoding"],"zstd")

    async def test_orphan_tool_output_is_not_fabricated_or_repaired(self):
        doc=self.document();doc["input"].append({"type":"function_call_output","id":"synthetic-orphan","output":"synthetic-context"})
        await self.request(doc)
        self.assertEqual(json.loads(self.observed[0]["body"])["input"][:-1],doc["input"])

    async def test_invalid_credential_rejected_before_upstream(self):
        h=self.headers();h["Authorization"]="Bearer synthetic-wrong"
        status,_,body=await self.request(headers=h)
        self.assertEqual(status,401);self.assertFalse(self.observed)
        self.assertEqual(json.loads(body)["error"]["code"],"invalid_client_credential")

    async def test_foreign_origin_rejected_before_auth_or_upstream(self):
        self.client_file.unlink();h=self.headers();h["Origin"]="https://example.invalid"
        status,_,_=await self.request(headers=h)
        self.assertEqual(status,403);self.assertFalse(self.observed)

    async def test_foreign_host_and_cross_site_fetch_are_rejected(self):
        for key,value in (("Host","example.invalid"),("Sec-Fetch-Site","cross-site")):
            h=self.headers();h[key]=value
            status,_,_=await self.request(headers=h)
            self.assertEqual(status,403)
        self.assertFalse(self.observed)

    async def test_only_upstream_credential_is_forwarded(self):
        await self.request()
        h=self.observed[0]["headers"]
        self.assertEqual(h["Authorization"],"Bearer "+self.UPSTREAM_TOKEN)
        for name in ("Cookie","ChatGPT-Account-Id","X-API-Key"):self.assertNotIn(name,h)
        self.assertNotIn(self.CLIENT_TOKEN,str(h))
        self.assertNotIn(self.CLIENT_TOKEN,json.dumps(self.events))
        self.assertNotIn(self.UPSTREAM_TOKEN,json.dumps(self.events))

    async def test_upstream_redirect_is_blocked_without_retry(self):
        self.upstream_status=302;self.upstream_body=b"";self.upstream_headers["Location"]="http://127.0.0.1:9/must-not-follow"
        status,_,body=await self.request()
        self.assertEqual(status,502);self.assertEqual(len(self.observed),1)
        self.assertEqual(json.loads(body)["error"]["code"],"upstream_redirect_blocked")

    async def test_schema_error_is_observed_without_private_error_text(self):
        secret="synthetic-private-argument-value"
        self.error_stream("basispoints function arguments do not satisfy the declared tool schema or validation resource limits; "+secret)
        status,_,body=await self.request()
        self.assertEqual((status,body),(200,self.upstream_body))
        self.assertEqual(self.events[-1]["upstream_error_diagnostics"]["errors"][0]["category"],"schema_or_resource")
        self.assertNotIn(secret,json.dumps(self.events))

    async def test_catalog_error_is_classified_and_sse_unchanged(self):
        self.error_stream("basispoints returned a tool outside the client's catalog; diag=3; tool=update_plan; transport=run_officejs")
        _,_,body=await self.request()
        self.assertEqual(body,self.upstream_body)
        e=self.events[-1]["upstream_error_diagnostics"]["errors"][0]
        self.assertEqual((e["category"],e["tool"]),("tool_outside_client_catalog","update_plan"))
        self.assertEqual(len(self.observed),1)

    async def test_observer_fault_never_changes_bytes_or_retries(self):
        class BrokenObserver:
            def feed(self,chunk):raise RuntimeError("synthetic fault")
            def finish(self):raise AssertionError("disabled observer must not finish")
        with mock.patch("auth_bridge.SSEErrorObserver",BrokenObserver):
            status,_,body=await self.request()
        self.assertEqual((status,body),(200,self.SSE_BYTES));self.assertEqual(len(self.observed),1)
        self.assertTrue(self.events[-1]["diagnostic_observer_fault"])
        self.assertEqual(self.events[-1]["upstream_error_diagnostics"],{})

    async def test_encoded_sse_and_non_sse_have_no_observation(self):
        self.error_stream("basispoints returned a tool outside the client's catalog; tool=update_plan")
        self.upstream_body=gzip.compress(self.upstream_body);self.upstream_headers["Content-Encoding"]="gzip"
        status,headers,body=await self.request()
        self.assertEqual((status,body),(200,self.upstream_body));self.assertEqual(headers["Content-Encoding"],"gzip")
        self.assertEqual(self.events[-1]["upstream_error_diagnostics"],{})
        del self.upstream_headers["Content-Encoding"]
        self.upstream_headers["Content-Type"]="application/json";self.upstream_body=b'{"synthetic":"ordinary response"}'
        _,_,body=await self.request()
        self.assertEqual(body,self.upstream_body);self.assertEqual(self.events[-1]["upstream_error_diagnostics"],{})

    async def test_outside_model_passes_without_observer(self):
        self.error_stream("basispoints function arguments do not satisfy the declared tool schema or validation resource limits")
        doc=self.document();doc["model"]="synthetic-unconfigured-model"
        raw=json.dumps(doc,indent=3).encode()
        _,_,body=await self.request(raw=raw)
        self.assertEqual(body,self.upstream_body);self.assertEqual(self.observed[0]["body"],raw)
        self.assertEqual(self.events[-1]["upstream_error_diagnostics"],{})

    async def test_websocket_negotiation_authenticates_before_426(self):
        h=self.headers();h.update({"Connection":"Upgrade","Upgrade":"websocket"})
        status,_,_=await self.request(headers=h,method="GET",raw=b"")
        self.assertEqual(status,426)
        h["Authorization"]="Bearer synthetic-wrong"
        status,_,_=await self.request(headers=h,method="GET",raw=b"")
        self.assertEqual(status,401);self.assertFalse(self.observed)

    async def test_unknown_path_and_method_rejected(self):
        for path,method,expected in (("/not-allowed","POST",404),("/v1/models","POST",405)):
            status,_,_=await self.request(path=path,method=method)
            self.assertEqual(status,expected)
        self.assertFalse(self.observed)

    async def test_query_is_forwarded_but_not_logged(self):
        _,_,body=await self.request(path="/v1/responses?synthetic_query=private")
        self.assertEqual(body,self.SSE_BYTES)
        self.assertEqual(self.observed[0]["path_qs"],"/v1/responses?synthetic_query=private")
        self.assertNotIn("synthetic_query",json.dumps(self.events))

    async def test_models_get_passes_without_guard(self):
        self.upstream_headers["Content-Type"]="application/json"
        self.upstream_body=b'{"data":[{"id":"synthetic-model"}]}'
        status,_,body=await self.request(method="GET",path="/v1/models",raw=b"")
        self.assertEqual((status,body),(200,self.upstream_body))
        self.assertEqual(self.events[-1]["catalog_guard_added"],0)

    async def test_missing_explicit_credentials_fails_without_upstream(self):
        self.client_file.unlink()
        status,_,_=await self.request()
        self.assertEqual(status,503);self.assertFalse(self.observed)
