"""One URL in, three legacy keys out — total over the documented class, loud on everything else.

`BLOODHOUND_DOMAIN`, `BLOODHOUND_PORT` and `BLOODHOUND_SCHEME` express a single fact: where
BloodHound is. Three fields is three chances to get it wrong, and `DOMAIN` is a **hostname**, which
is a confusing name inside a tool whose entire subject is Active Directory domains. Sage now takes
one URL and expands it at the subprocess boundary, because the MCP server still reads the three.

Property-shaped rather than example-shaped on purpose: the input space is scheme × port × host-form ×
trailing-slash, and a handful of hand-picked strings would cover a corner of it while reading as
thorough. The rejection cases matter as much as the accepting ones — silently dropping `/ui/login`
from a pasted browser URL yields a configuration that looks like what the operator typed and is not.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.bloodhound_config import (  # noqa: E402
    BLOODHOUND_URL_KEY,
    BloodHoundURLError,
    parse_bloodhound_url,
)

SCHEMES = [("", "http", "80"), ("http://", "http", "80"), ("https://", "https", "443")]
HOSTS = ["bloodhound.example", "127.0.0.1", "[::1]"]
EXPECTED_HOST = {"bloodhound.example": "bloodhound.example", "127.0.0.1": "127.0.0.1", "[::1]": "::1"}


@pytest.mark.parametrize(
    "scheme_prefix,expected_scheme,default_port,host,port,trailing",
    [
        (prefix, scheme, default, host, port, trailing)
        for (prefix, scheme, default), host, port, trailing in itertools.product(
            SCHEMES, HOSTS, ["", "8083"], ["", "/"]
        )
    ],
)
def test_parser_is_total_over_the_documented_class(
    scheme_prefix, expected_scheme, default_port, host, port, trailing
):
    """36 combinations of scheme, host form, explicit port and trailing slash."""
    value = f"{scheme_prefix}{host}" + (f":{port}" if port else "") + trailing

    parsed = parse_bloodhound_url(value)

    assert parsed["BLOODHOUND_SCHEME"] == expected_scheme
    assert parsed["BLOODHOUND_DOMAIN"] == EXPECTED_HOST[host]
    assert parsed["BLOODHOUND_PORT"] == (port or default_port)
    assert set(parsed) == {"BLOODHOUND_DOMAIN", "BLOODHOUND_PORT", "BLOODHOUND_SCHEME"}


@pytest.mark.parametrize(
    "value,offending",
    [
        ("http://bh.example/ui/login", "a path"),
        ("http://bh.example:8083/api/v2", "a path"),
        ("http://bh.example?token=abc", "a query string"),
        ("http://bh.example#section", "a fragment"),
        ("http://user:secret@bh.example", "embedded credentials"),
    ],
)
def test_rejections_name_the_offending_part(value, offending):
    """A refusal that does not say what it objected to is barely better than a silent default."""
    with pytest.raises(BloodHoundURLError) as raised:
        parse_bloodhound_url(value)

    message = str(raised.value)
    assert offending in message
    assert BLOODHOUND_URL_KEY in message


@pytest.mark.parametrize("value", ["", "   ", "http://", "ftp://bh.example", "http://bh.example:notaport"])
def test_unusable_values_are_refused_rather_than_guessed(value):
    with pytest.raises(BloodHoundURLError):
        parse_bloodhound_url(value)


def test_a_pasted_browser_url_fails_loudly_rather_than_silently_working():
    """The realistic operator mistake, called out on its own because it is the likely one."""
    with pytest.raises(BloodHoundURLError) as raised:
        parse_bloodhound_url("http://127.0.0.1:8083/ui/login")

    assert "a path" in str(raised.value)


def test_credentials_in_a_url_are_never_echoed_back():
    """The refusal message quotes the input, so a secret in it would land in a log."""
    with pytest.raises(BloodHoundURLError) as raised:
        parse_bloodhound_url("http://user:hunter2@bh.example")

    assert "hunter2" not in str(raised.value), "a rejection message leaked the credential it rejected"
