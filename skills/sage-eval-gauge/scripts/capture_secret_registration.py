#!/usr/bin/env python3
"""Register explicit operator values and native credentials in evaluator memory."""
from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _module(name: str):
    spec = importlib.util.spec_from_file_location("sage_registration_" + name,
                                                 Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_redaction = _module("capture_redaction")
_sources = _module("capture_sink_sources")


class SecretRegistrationError(ValueError):
    pass


def register_secrets(operator_values: Mapping[str, str | bytes],
                     first: Mapping[str, Any], final: Mapping[str, Any]):
    """The caller supplies only explicit in-scope authentication values.

    No environment discovery, network access, durable secret file, or claim that
    arbitrary unregistered secrets have been detected is made by this helper.
    Both origins are retained when an operator and runtime value happen to match.
    """
    if not isinstance(operator_values, Mapping) or not operator_values:
        raise SecretRegistrationError("operator secret inventory is missing")
    try:
        first_operation, first_rows, _ = _sources._snapshot(first)
        final_operation, final_rows, _ = _sources._snapshot(final)
        if first_operation != final_operation:
            raise SecretRegistrationError("native operation changed")
        index = _redaction.SecretIndex()
        for handle, value in operator_values.items():
            index.register("operator." + handle, value, "operator")
        for phase, rows in (("start", first_rows), ("terminal", final_rows)):
            for row in rows["credential"]:
                value = row["credential"]
                if not isinstance(value, str) or not value:
                    raise SecretRegistrationError("native credential value is unavailable")
                index.register(f"native.{phase}.{row['id']}", value, "runtime")
    except (TypeError, ValueError, KeyError):
        raise SecretRegistrationError("secret registration source is invalid") from None
    return index


if __name__ == "__main__":
    print("DRY RUN: register explicit values and complete native snapshots in memory.")
