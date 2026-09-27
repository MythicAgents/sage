#!/usr/bin/env python3
"""In-memory exact-secret redaction for prospective evaluator capture."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote_from_bytes


_HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CATEGORIES = frozenset({"operator", "runtime"})
_MARKER = b"<SAGE_REDACTED>"


class RedactionError(ValueError):
    pass


class SecretIndex:
    """Positive secret inventory whose raw values never leave this object."""

    def __init__(self) -> None:
        self._handles: dict[str, tuple[bytes, str]] = {}
        self._patterns: dict[bytes, list[tuple[str, str, str]]] = defaultdict(list)

    def __repr__(self) -> str:
        return f"SecretIndex(registrations={len(self._handles)}, encodings=exact-supported-only)"

    def register(self, handle: str, value: str | bytes, category: str) -> None:
        if not isinstance(handle, str) or not _HANDLE.fullmatch(handle) or handle in self._handles \
                or category not in _CATEGORIES or not isinstance(value, (str, bytes)) or not value:
            raise RedactionError("secret registration is invalid")
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if not raw:
            raise RedactionError("secret registration is invalid")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        encodings: list[tuple[str, bytes]] = [("raw_utf8", raw)]
        if text is not None:
            encodings.extend((
                ("utf16le", text.encode("utf-16le")),
                ("json_escaped", json.dumps(text, ensure_ascii=True)[1:-1].encode("ascii")),
            ))
        encodings.append(("percent_encoded", quote_from_bytes(raw, safe="").encode("ascii")))
        for name, encoder in (
            ("base64_standard", base64.b64encode), ("base64_urlsafe", base64.urlsafe_b64encode),
        ):
            encoded = encoder(raw)
            encodings.append((name + "_padded", encoded))
            if encoded.endswith(b"="):
                encodings.append((name + "_unpadded", encoded.rstrip(b"=")))
        self._handles[handle] = (raw, category)
        for provenance, pattern in encodings:
            if pattern:
                self._patterns[pattern].append((handle, category, provenance))

    def scan(self, content: bytes) -> list[dict[str, Any]]:
        if not isinstance(content, bytes):
            raise RedactionError("redaction input is invalid")
        found: dict[tuple[int, int], set[tuple[str, str, str]]] = defaultdict(set)
        for pattern, identities in self._patterns.items():
            start = 0
            while True:
                start = content.find(pattern, start)
                if start < 0:
                    break
                found[(start, start + len(pattern))].update(identities); start += 1
        return [
            {"start": start, "end": end, "length": end - start,
             "identities": [{"handle": handle, "category": category, "encoding": encoding}
                            for handle, category, encoding in sorted(identities)]}
            for (start, end), identities in sorted(found.items(), key=lambda item: (item[0][0], -item[0][1]))
        ]

    def redact_bytes(self, content: bytes) -> dict[str, Any]:
        matches = self.scan(content)
        intervals: list[list[int]] = []
        for match in matches:
            start, end = match["start"], match["end"]
            if intervals and start < intervals[-1][1]:
                intervals[-1][1] = max(intervals[-1][1], end)
            else:
                intervals.append([start, end])
        output = bytearray(); cursor = 0
        for start, end in intervals:
            output.extend(content[cursor:start]); output.extend(_MARKER); cursor = end
        output.extend(content[cursor:])
        return {
            "schema": "sage-redacted-bytes-v1", "redacted": bytes(output),
            "metadata": {"original_sha256": hashlib.sha256(content).hexdigest(),
                         "original_length": len(content), "matches": matches},
        }

    def redact_json(self, value: Any) -> Any:
        """Return a JSON-safe recursively redacted value."""
        if isinstance(value, str):
            return self.redact_bytes(value.encode("utf-8"))["redacted"].decode("utf-8")
        if isinstance(value, bytes):
            redacted = self.redact_bytes(value)
            return {"schema": "sage-redacted-bytes-json-v1",
                    "base64": base64.b64encode(redacted["redacted"]).decode("ascii"),
                    "metadata": redacted["metadata"]}
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(key, str):
                    redacted_key = self.redact_json(key)
                elif isinstance(key, bytes):
                    redacted_key = "sage-redacted-bytes-key-v1:" + base64.b64encode(
                        self.redact_bytes(key)["redacted"]
                    ).decode("ascii")
                else:
                    raise RedactionError("redaction input is invalid")
                if redacted_key in result:
                    raise RedactionError("redacted key collision")
                result[redacted_key] = self.redact_json(item)
            return result
        if isinstance(value, (list, tuple)):
            return [self.redact_json(item) for item in value]
        if value is None or isinstance(value, (bool, int)) \
                or isinstance(value, float) and math.isfinite(value):
            return value
        raise RedactionError("redaction input is invalid")
