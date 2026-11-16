"""Synthetic privacy, framing and resource tests; no services or credentials."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tracemalloc
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from error_observer import (
    CATEGORIES, CODES, DEFAULT_TOOL_NAMES, MAX_ERROR_BUCKETS, MAX_EVENT_BYTES, MAX_MESSAGE_BYTES, MAX_TOOL_NAMES,
    SSEErrorObserver, TRANSPORTS,
)

SCHEMA_MESSAGE = "basispoints function arguments do not satisfy the declared tool schema or validation resource limits"
OUTSIDE_MESSAGE = "basispoints returned a tool outside the client's catalog; diag=3; tool=update_plan; transport=run_officejs"


def failure(message=SCHEMA_MESSAGE, code="basispoints_protocol_error", *, event="response.failed", named=True, newline="\n", complete=True):
    document = {"type": event, "response": {"error": {"code": code, "message": message}}} if event == "response.failed" else {"type": event, "error": {"code": code, "message": message}}
    prefix = "event: " + event + newline if named else ""
    raw = prefix + "data: " + json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    if complete:
        raw += newline + newline
    return raw.encode("utf-8")


def observe(raw, chunks=None, *, allowed_tool_names=()):
    observer = SSEErrorObserver(allowed_tool_names=allowed_tool_names)
    if chunks is None:
        observer.feed(raw)
    else:
        for start in range(0, len(raw), chunks):
            observer.feed(raw[start:start + chunks])
    return observer.finish()


def padded_failure(size, *, ending=b"\r\n\r\n"):
    event = failure(newline="\r\n", complete=False) + ending
    padding = size - len(event) - 3  # Comment colon plus CRLF.
    if padding < 0:
        raise ValueError("event_size_too_small")
    return b":" + b"x" * padding + b"\r\n" + event


class SSEErrorObserverTests(unittest.TestCase):
    def test_schema_failure_has_no_inferred_tool_or_field(self):
        summary = observe(failure(SCHEMA_MESSAGE + "; tool=synthetic_secret; path=arguments.secret"))
        self.assertEqual(summary, {"errors": [{"category": "schema_or_resource", "code": "basispoints_protocol_error", "count": 1}], "error_events": 1, "skipped_events": 0, "overflow_errors": 0})

    def test_current_update_plan_error_extracts_only_safe_metadata(self):
        summary = observe(failure(OUTSIDE_MESSAGE))
        self.assertEqual(summary["errors"], [{"category": "tool_outside_client_catalog", "code": "basispoints_protocol_error", "tool": "update_plan", "transport": "run_officejs", "diag": 3, "count": 1}])

    def test_native_not_in_catalog_classification(self):
        raw = failure("basispoints returned an unsupported native tool; no tool was executed; diag=3; tool=functions.list_connectors; type=function_call; reason=not_in_client_catalog; catalog_count=171")
        item = observe(raw)["errors"][0]
        self.assertEqual(item["category"], "native_not_in_catalog")
        self.assertEqual(item["tool"], "functions.list_connectors")
        self.assertEqual(item["diag"], 3)
        self.assertNotIn("catalog_count", item)

    def test_all_fixed_categories_are_reachable(self):
        cases = [
            ("basispoints returned an unsupported native tool; reason=not_in_client_catalog", "basispoints_protocol_error", "native_not_in_catalog"),
            (OUTSIDE_MESSAGE, "basispoints_protocol_error", "tool_outside_client_catalog"),
            (SCHEMA_MESSAGE, "basispoints_protocol_error", "schema_or_resource"),
            ("bps_request_unsupported; reason=missing_tool_call; path=input[359]; hint=resend_complete_tool_call_and_result", "bps_request_unsupported", "missing_tool_call"),
            ("basispoints tool transport remains invalid after 2 corrections; no tool was executed: synthetic reason", "basispoints_protocol_error", "transport_invalid"),
            ("bps_request_unsupported; reason=tool_catalog; path=tools", "bps_request_unsupported", "request_unsupported"),
            ("Synthetic unknown error body", "synthetic-unknown-code", "unknown_error"),
        ]
        self.assertEqual({expected for _, _, expected in cases}, set(CATEGORIES))
        for message, code, expected in cases:
            with self.subTest(category=expected):
                item = observe(failure(message, code))["errors"][0]
                self.assertEqual(item["category"], expected)
                self.assertIn(item["code"], CODES)
                if expected == "missing_tool_call":
                    self.assertEqual(item["input_index"], 359)

    def test_unknown_dynamic_code_is_not_returned(self):
        summary = observe(failure("synthetic message contains token-should-not-appear", "synthetic-secret-code"))
        self.assertEqual(summary["errors"], [{"category": "unknown_error", "code": "unknown", "count": 1}])
        self.assertNotIn("synthetic-secret", json.dumps(summary))

    def test_utf8_and_crlf_split_at_every_byte(self):
        raw = failure(OUTSIDE_MESSAGE + "; ignored=中文测试", newline="\r\n")
        self.assertEqual(observe(raw, 1), observe(raw))
        self.assertEqual(observe(raw, 7)["errors"][0]["tool"], "update_plan")

    def test_cr_lf_and_mixed_line_endings(self):
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=newline):
                self.assertEqual(observe(failure(newline=newline), 1)["error_events"], 1)
        raw = b'event: error\r\ndata: {"type":"error",\rdata: "message":"synthetic"}\n\r\n'
        self.assertEqual(observe(raw, 1)["error_events"], 1)

    def test_multidata_lines_form_one_json_event(self):
        raw = b'event: response.failed\n: synthetic comment\ndata: {"type":"response.failed",\ndata: "response":{"error":{"code":"basispoints_protocol_error",\ndata: "message":"' + SCHEMA_MESSAGE.encode() + b'"}}}\n\n'
        self.assertEqual(observe(raw, 11)["errors"][0]["category"], "schema_or_resource")

    def test_utf8_bom_at_stream_start(self):
        raw = b"\xef\xbb\xbf" + failure()
        self.assertEqual(observe(raw, 1)["error_events"], 1)

    def test_data_only_error_envelope_is_supported(self):
        self.assertEqual(observe(failure(named=False))["error_events"], 1)

    def test_error_event_without_type_supports_direct_fields(self):
        raw = b'event: error\ndata: {"code":"bps_request_unsupported","message":"bps_request_unsupported; reason=missing_tool_call; path=input[12]"}\n\n'
        item = observe(raw)["errors"][0]
        self.assertEqual((item["category"], item["input_index"]), ("missing_tool_call", 12))

    def test_ordinary_deltas_and_final_text_are_not_recorded(self):
        documents = [
            {"type": "response.output_text.delta", "delta": SCHEMA_MESSAGE},
            {"type": "response.function_call_arguments.delta", "delta": '{"token":"synthetic-private"}'},
            {"type": "response.completed", "response": {"output": [{"text": OUTSIDE_MESSAGE}]}},
        ]
        raw = b"".join(("data: " + json.dumps(doc) + "\n\n").encode() for doc in documents) + b"data: [DONE]\n\n"
        self.assertEqual(observe(raw), {"errors": [], "error_events": 0, "skipped_events": 0, "overflow_errors": 0})

    def test_named_nonerror_event_does_not_trust_embedded_error_type(self):
        raw = failure().replace(b"event: response.failed", b"event: response.output_text.delta")
        self.assertEqual(observe(raw)["error_events"], 0)
        self.assertEqual(observe(raw)["skipped_events"], 0)

    def test_conflicting_error_event_and_json_type_is_skipped(self):
        raw = failure().replace(b"event: response.failed", b"event: error")
        summary = observe(raw)
        self.assertEqual((summary["error_events"], summary["skipped_events"]), (0, 1))

    def test_bad_json_and_duplicate_keys_recover_at_event_boundary(self):
        for invalid in (b'data: {invalid}\n\n', b'event: error\ndata: {"type":"error","message":"a","message":"b"}\n\n'):
            summary = observe(invalid + failure(OUTSIDE_MESSAGE), 3)
            self.assertEqual((summary["error_events"], summary["skipped_events"]), (1, 1))
            self.assertEqual(summary["errors"][0]["tool"], "update_plan")

    def test_invalid_utf8_event_does_not_break_later_error(self):
        summary = observe(b'event: error\ndata: {"message":"\xff"}\n\n' + failure())
        self.assertEqual((summary["error_events"], summary["skipped_events"]), (1, 1))

    def test_oversize_event_discards_through_boundary_then_recovers(self):
        oversized = b'event: error\ndata: ' + b'x' * (MAX_EVENT_BYTES + 1) + b'\r\ndata: still same event\r\n\r\n'
        summary = observe(oversized + failure(OUTSIDE_MESSAGE), 17)
        self.assertEqual((summary["error_events"], summary["skipped_events"]), (1, 1))
        self.assertEqual(summary["errors"][0]["tool"], "update_plan")

    def test_crlf_event_byte_boundary_is_independent_of_chunks(self):
        for size in (MAX_EVENT_BYTES - 1, MAX_EVENT_BYTES, MAX_EVENT_BYTES + 1):
            raw = padded_failure(size)
            expected = (1, 0) if size <= MAX_EVENT_BYTES else (0, 1)
            for chunks in (None, 1, 2, 3, 7, 64, MAX_EVENT_BYTES):
                with self.subTest(size=size, chunks=chunks):
                    summary = observe(raw, chunks)
                    self.assertEqual((summary["error_events"], summary["skipped_events"]), expected)

    def test_split_crlf_and_empty_chunks_preserve_event_budget(self):
        for size in (MAX_EVENT_BYTES - 1, MAX_EVENT_BYTES, MAX_EVENT_BYTES + 1):
            raw = padded_failure(size)
            expected = (1, 0) if size <= MAX_EVENT_BYTES else (0, 1)
            splits = [index + 1 for index, value in enumerate(raw[:-1])
                      if value == 13 and raw[index + 1] == 10]
            for split in splits:
                with self.subTest(size=size, split=split):
                    observer = SSEErrorObserver()
                    observer.feed(raw[:split])
                    observer.feed(b"")
                    observer.feed(raw[split:])
                    summary = observer.finish()
                    self.assertEqual((summary["error_events"], summary["skipped_events"]), expected)

    def test_eof_lone_cr_counts_toward_event_budget(self):
        for ending in (b"\r", b"\r\n\r"):
            for size in (MAX_EVENT_BYTES - 1, MAX_EVENT_BYTES, MAX_EVENT_BYTES + 1):
                raw = padded_failure(size, ending=ending)
                expected = (1, 0) if size <= MAX_EVENT_BYTES else (0, 1)
                for chunks in (None, 1, 7):
                    with self.subTest(ending=ending, size=size, chunks=chunks):
                        summary = observe(raw, chunks)
                        self.assertEqual((summary["error_events"], summary["skipped_events"]), expected)

    def test_oversize_split_crlf_recovers_at_next_event(self):
        raw = padded_failure(MAX_EVENT_BYTES + 1) + failure(OUTSIDE_MESSAGE, newline="\r\n")
        for chunks in (None, 1, 2, 7, MAX_EVENT_BYTES):
            with self.subTest(chunks=chunks):
                summary = observe(raw, chunks)
                self.assertEqual((summary["error_events"], summary["skipped_events"]), (1, 1))
                self.assertEqual(summary["errors"][0]["tool"], "update_plan")

    def test_adjacent_exact_limit_events_charge_crlf_to_correct_event(self):
        raw = padded_failure(MAX_EVENT_BYTES) * 2
        for chunks in (None, 1, 7, MAX_EVENT_BYTES - 1, MAX_EVENT_BYTES):
            with self.subTest(chunks=chunks):
                summary = observe(raw, chunks)
                self.assertEqual((summary["error_events"], summary["skipped_events"]), (2, 0))
                self.assertEqual(summary["errors"][0]["count"], 2)

    def test_many_data_lines_cannot_bypass_event_budget(self):
        oversized = b'event: error\n' + b'data: ' + b'x' * 1024 + b'\n'
        oversized += (b'data: ' + b'x' * 1024 + b'\n') * 70 + b'\n'
        summary = observe(oversized + failure())
        self.assertEqual((summary["error_events"], summary["skipped_events"]), (1, 1))

    def test_huge_single_chunk_keeps_observer_allocation_bounded(self):
        chunk = b'data: ' + b'x' * (4 * 1024 * 1024) + b'\n\n'
        observer = SSEErrorObserver()
        tracemalloc.start()
        try:
            observer.feed(chunk)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)
        observer.feed(failure())
        summary = observer.finish()
        self.assertEqual((summary["error_events"], summary["skipped_events"]), (1, 1))

    def test_message_byte_limit_rejects_whole_error_not_truncated_metadata(self):
        for message in ("x" * (MAX_MESSAGE_BYTES + 1), "中" * 3000):
            summary = observe(failure(message) + failure())
            self.assertEqual((summary["error_events"], summary["skipped_events"]), (1, 1))

    def test_tool_name_ascii_length_and_credential_shape_guards(self):
        values = ["中文", "tool/with/slash", "tool with space", "tool\nline", "a" * 257, "sk_synthetic_secret", "github_pat_synthetic", "eyJsynthetic.payload.signature"]
        for tool in values:
            item = observe(failure("basispoints returned a tool outside the client's catalog; tool=" + tool))["errors"][0]
            self.assertNotIn("tool", item)
        safe = "a" * 256
        self.assertEqual(observe(failure("basispoints returned a tool outside the client's catalog; tool=" + safe), allowed_tool_names=[safe])["errors"][0]["tool"], safe)

    def test_transport_is_allowlisted_and_integer_metadata_bounded(self):
        item = observe(failure("basispoints returned a tool outside the client's catalog; tool=update_plan; transport=synthetic-secret; diag=999999999999999999"))["errors"][0]
        self.assertNotIn("transport", item)
        self.assertNotIn("diag", item)
        for path in ("input[-1]", "input[999999999999999999]", "input[12].arguments", "arguments.secret"):
            item = observe(failure("bps_request_unsupported; reason=missing_tool_call; path=" + path, "bps_request_unsupported"))["errors"][0]
            self.assertNotIn("input_index", item)
        for transport in TRANSPORTS:
            item = observe(failure("basispoints returned a tool outside the client's catalog; transport=" + transport))["errors"][0]
            self.assertEqual(item["transport"], transport)

    def test_duplicate_diagnostic_fields_are_not_guessed(self):
        item = observe(failure(OUTSIDE_MESSAGE + "; tool=other; diag=4; transport=CUSTOM"))["errors"][0]
        self.assertNotIn("tool", item)
        self.assertNotIn("diag", item)
        self.assertNotIn("transport", item)

    def test_raw_private_fields_never_appear_in_summary(self):
        secret = "SYNTHETIC_PRIVATE_SENTINEL_54831"
        document = {"type": "response.failed", "response": {"error": {"code": secret, "message": "unknown " + secret, "arguments": {"secret": secret}}, "output": [{"text": secret}], "headers": {"Authorization": secret}, "token": secret}}
        summary = observe(("data: " + json.dumps(document) + "\n\n").encode())
        encoded = json.dumps(summary)
        self.assertNotIn(secret, encoded)
        for forbidden in ("message", "arguments", "output", "headers", "token"):
            self.assertNotIn('"' + forbidden + '"', encoded)
        self.assertEqual(summary["errors"][0]["code"], "unknown")

    def test_repeated_errors_aggregate_without_growing_buckets(self):
        summary = observe(failure(OUTSIDE_MESSAGE) * 50)
        self.assertEqual(summary["error_events"], 50)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["errors"][0]["count"], 50)

    def test_distinct_error_buckets_are_bounded(self):
        names = ["synthetic_" + str(index) for index in range(MAX_ERROR_BUCKETS + 7)]
        raw = b"".join(failure("basispoints returned a tool outside the client's catalog; tool=" + name) for name in names)
        summary = observe(raw, allowed_tool_names=names)
        self.assertEqual(len(summary["errors"]), MAX_ERROR_BUCKETS)
        self.assertEqual(summary["error_events"], MAX_ERROR_BUCKETS + 7)
        self.assertEqual(summary["overflow_errors"], 7)

    def test_finish_is_idempotent_and_returns_independent_containers(self):
        observer = SSEErrorObserver()
        observer.feed(failure(OUTSIDE_MESSAGE, complete=False))
        first = observer.finish()
        self.assertEqual(first["error_events"], 1)
        first["errors"][0]["tool"] = "modified-outside"
        first["errors"].clear()
        self.assertEqual(observer.finish()["errors"][0]["tool"], "update_plan")
        observer.feed(failure())
        self.assertEqual(observer.finish()["error_events"], 1)

    def test_truncated_eof_event_is_skipped(self):
        summary = observe(b'event: response.failed\ndata: {"type":"response.failed",')
        self.assertEqual((summary["error_events"], summary["skipped_events"]), (0, 1))

    def test_nonbytes_feed_is_nonthrowing_and_later_stream_still_works(self):
        observer = SSEErrorObserver()
        for bad in (None, "data: not bytes", 123):
            observer.feed(bad)
        observer.feed(failure())
        summary = observer.finish()
        self.assertEqual((summary["error_events"], summary["skipped_events"]), (1, 3))

    def test_arbitrary_binary_stream_is_nonthrowing(self):
        observer = SSEErrorObserver()
        observer.feed(bytes(range(256)) * 512)
        observer.feed(b"\n\n" + failure())
        summary = observer.finish()
        self.assertIsInstance(summary, dict)
        self.assertEqual(summary["error_events"], 1)


    def test_full_tool_field_with_controls_never_becomes_a_valid_prefix(self):
        for tool in ("tool\nline", "tool\rline", "tool\tline", "tool\n", "tool\r", "tool\t", "tool\x7f", "SK_synthetic_credential"):
            with self.subTest(tool=repr(tool)):
                for suffix in ("", "; transport=run_officejs; diag=3"):
                    summary = observe(failure("basispoints returned a tool outside the client's catalog; tool=" + tool + suffix), 3, allowed_tool_names=["tool"])
                    self.assertEqual(summary["error_events"], 1)
                    self.assertNotIn("tool", summary["errors"][0])

    def test_default_public_tool_names_are_the_only_implicit_names(self):
        expected = {"update_plan", "list_connectors", "run_officejs", "functions.update_plan", "functions.list_connectors", "functions.run_officejs"}
        self.assertEqual(set(DEFAULT_TOOL_NAMES), expected)
        for name in expected:
            item = observe(failure("basispoints returned a tool outside the client's catalog; tool=" + name))["errors"][0]
            self.assertEqual(item["tool"], name)

    def test_secret_shaped_valid_identifiers_are_never_logged_by_default(self):
        secrets = ["AIzaSyntheticSecretMustNotBeLogged", "OrdinaryAsciiSecret_738291", "ghp_SYNTHETIC_TOKEN", "sk_SYNTHETIC_TOKEN", "eyJSyntheticToken.payload.signature", "private.arguments.password"]
        for secret in secrets:
            with self.subTest(secret=secret):
                for allowed in ((), [], set(), frozenset()):
                    summary = observe(failure("basispoints returned a tool outside the client's catalog; tool=" + secret + "; transport=run_officejs; diag=3"), allowed_tool_names=allowed)
                    self.assertNotIn("tool", summary["errors"][0])
                    self.assertNotIn(secret, json.dumps(summary))

    def test_explicit_catalog_allows_only_exact_qualified_names(self):
        name = "client.safe_namespace.synthetic_tool"
        for allowed in ([name], (name,), {name}, frozenset({name})):
            item = observe(failure("basispoints returned a tool outside the client's catalog; tool=" + name), allowed_tool_names=allowed)["errors"][0]
            self.assertEqual(item["tool"], name)
            for other in ("synthetic_tool", "functions.synthetic_tool", name + "_suffix"):
                item = observe(failure("basispoints returned a tool outside the client's catalog; tool=" + other), allowed_tool_names=allowed)["errors"][0]
                self.assertNotIn("tool", item)

    def test_untrusted_or_infinite_allowlist_iterables_are_not_consumed(self):
        def never_consume():
            raise AssertionError("iterator must never run")
            yield "synthetic_tool"
        class HostileList(list):
            def __iter__(self):
                raise AssertionError("subclass must never run")
        for allowed in (never_consume(), {"synthetic_tool": True}, "synthetic_tool", None, 123, HostileList(["synthetic_tool"])):
            observer = SSEErrorObserver(allowed_tool_names=allowed)
            observer.feed(failure("basispoints returned a tool outside the client's catalog; tool=synthetic_tool"))
            self.assertNotIn("tool", observer.finish()["errors"][0])

    def test_allowlist_count_limit_is_bounded_and_fails_closed(self):
        names = ["synthetic_" + str(index) for index in range(MAX_TOOL_NAMES - len(DEFAULT_TOOL_NAMES))]
        observer = SSEErrorObserver(allowed_tool_names=names)
        self.assertEqual(len(observer._allowed_tool_names), MAX_TOOL_NAMES)
        observer.feed(failure("basispoints returned a tool outside the client's catalog; tool=" + names[-1]))
        self.assertEqual(observer.finish()["errors"][0]["tool"], names[-1])
        for oversized in (names + ["one_too_many"], ["synthetic_tool"] * (MAX_TOOL_NAMES + 1)):
            observer = SSEErrorObserver(allowed_tool_names=oversized)
            self.assertEqual(observer._allowed_tool_names, DEFAULT_TOOL_NAMES)
            observer.feed(failure("basispoints returned a tool outside the client's catalog; tool=" + names[0]))
            self.assertNotIn("tool", observer.finish()["errors"][0])

    def test_explicit_catalog_does_not_bypass_full_name_validation(self):
        class HostileString(str):
            def __hash__(self):
                raise AssertionError("subclass hash must never run")
        bad_names = ["", "x" * 257, "中文", "tool/slash", "tool\nline", "tool\r", "tool\t", "tool\x7f", 42, None, HostileString("synthetic_tool")]
        observer = SSEErrorObserver(allowed_tool_names=bad_names + ["client.valid_tool"])
        self.assertEqual(observer._allowed_tool_names, DEFAULT_TOOL_NAMES | {"client.valid_tool"})
        for name in bad_names[:8]:
            observer.feed(failure("basispoints returned a tool outside the client's catalog; tool=" + name))
        self.assertTrue(all("tool" not in item for item in observer.finish()["errors"]))

    def test_allowed_catalog_is_snapshotted_and_unknown_names_aggregate(self):
        supplied = ["client.valid_tool"]
        observer = SSEErrorObserver(allowed_tool_names=supplied)
        supplied.append("SecretAddedLater")
        for name in ("SecretAddedLater", "AnotherUnknownAsciiSecret"):
            observer.feed(failure("basispoints returned a tool outside the client's catalog; tool=" + name))
        summary = observer.finish()
        self.assertEqual(summary["error_events"], 2)
        self.assertEqual(summary["errors"], [{"category": "tool_outside_client_catalog", "code": "basispoints_protocol_error", "count": 2}])

    def test_counters_saturate_without_unbounded_integer_growth(self):
        with mock.patch("error_observer.MAX_COUNTER", 2):
            summary = observe(failure() * 5)
        self.assertEqual(summary["error_events"], 2)
        self.assertEqual(summary["errors"][0]["count"], 2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
