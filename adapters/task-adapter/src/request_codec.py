"""Bounded request decoding and strict JSON helpers, with no history repairs."""
from __future__ import annotations
import math
import zlib
from decimal import Decimal
from compression import zstd

INTEGRITY_HEADERS = ("Content-Digest", "Repr-Digest", "Digest", "Content-MD5", "Signature", "Signature-Input")


def _decode_bounded(body: bytes, encoding: str, limit: int) -> bytes:
    if encoding in ("", "identity"):
        if len(body) > limit:
            raise ValueError("body_limit")
        return body
    if encoding == "zstd":
        dec = zstd.ZstdDecompressor()
        decoded = dec.decompress(body, limit + 1)
        if len(decoded) > limit or not dec.eof or dec.unused_data:
            raise ValueError("unsupported_or_oversized_frame")
        return decoded
    if encoding == "gzip":
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = dec.decompress(body, limit + 1)
        if len(decoded) > limit or not dec.eof or dec.unused_data or dec.unconsumed_tail:
            raise ValueError("unsupported_or_oversized_frame")
        return decoded
    raise ValueError("unsupported_encoding")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _exact_float(text):
    value = float(text)
    if not math.isfinite(value) or Decimal(repr(value)) != Decimal(text):
        raise ValueError("non_roundtrippable_number")
    return value


def _reject_constant(text):
    raise ValueError("non_json_constant")


