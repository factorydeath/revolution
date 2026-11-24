from __future__ import annotations


import pytest


from attack import (
    known_tokens,
    make_formatter,
    unresolved_tokens,
    variants,
)
from db import parse_phone


@pytest.mark.parametrize(
    "raw,digits,country",
    [
        ("+79161234567", "79161234567", "RU"),
        ("8 (916) 123-45-67", "79161234567", "RU"),
        ("9161234567", "79161234567", "RU"),
        ("+380 (67) 123-45-67", "380671234567", "UA"),
        ("380671234567", "380671234567", "UA"),
        ("0671234567", "380671234567", "UA"),
        ("671234567", "380671234567", "UA"),
        ("", "", "RU"),
    ],
)
def test_parse(raw, digits, country):
    assert parse_phone(raw) == (digits, country)


def test_variants_ru():
    table = variants("79161234567")
    assert table == {
        "RAW": "79161234567",
        "PLUS": "+79161234567",
        "NAT": "9161234567",
        "LAST10": "9161234567",
        "RU1": "+7 (916) 123-45-67",
        "RU2": "7(916) 123-45-67",
        "RU3": "+7(916)123-45-67",
        "RU3URL": "%2B7(916)123-45-67",
        "RU4": "+7(916) 123-45-67",
        "RU5": "+7 916 123 45 67",
        "RU6": "(916) 123-45-67",
        "RU7": "8(916) 123-45-67",
        "RU8": "+7(916) 123-4567",
        "RUDASH": "916 123-45-67",
    }


def test_variants_ua():
    table = variants("380671234567")
    assert table == {
        "RAW": "380671234567",
        "PLUS": "+380671234567",
        "NAT": "80671234567",
        "LAST10": "0671234567",
        "UALOCAL": "0671234567",
        "UA10": "671234567",
        "UA1": "+38 067 123 45 67",
        "UA2": "+380(67)123-45-67",
        "UA2DASH": "+380(67)-123-45-67",
        "UA2B": "+380 (67) 123-45-67",
        "UA3": "+38(067) 123-45-67",
        "UA4": "+38(067)1234567",
        "UA5": "+38(067) 123-4567",
        "UA6": "+38 (067) 123-45-67",
        "UA7": "+380(067)123-45-67",
        "UA8": "+38 (067) 123 45 67",
        "UA9": "+380 67 123 45 67",
        "FMTUA2": "+38(067)123-45-67",
        "UASPACE": "+380 67 123 45 67",
        "UAPAREN": "+38(067) 123 45 67",
        "UA_PAREN": "(067) 123-45-67",
        "UA_PAREN_PLUS": "(067)+123-45-67",
    }


def test_formatter_single_pass_and_unknown():
    fmt = make_formatter("79161234567")
    assert fmt("{{PLUS}}") == "+79161234567"

    assert fmt("{{RAW}}{{NOPE}}") == "79161234567{{NOPE}}"
    assert fmt("") == ""
    assert unresolved_tokens(fmt("{{PLUS}}")) == set()
    assert unresolved_tokens(fmt("{{NOPE}}")) == {"NOPE"}


def test_formatter_extra_tokens():
    fmt = make_formatter("79161234567", {"CHALLENGE": "abc"})
    assert fmt("{{CHALLENGE}}/{{RAW}}") == "abc/79161234567"
    assert make_formatter("79161234567")("{{CHALLENGE}}") == "{{CHALLENGE}}"


def test_all_known_tokens_resolve():
    for digits in ("79161234567", "380671234567"):
        fmt = make_formatter(digits)
        for token in known_tokens():
            if token in variants(digits):
                assert "{{" not in fmt("{{" + token + "}}")


def test_short_numbers_do_not_crash():
    assert variants("380")["RAW"] == "380"
    assert variants("7") == {"RAW": "7", "PLUS": "+7", "LAST10": "7"}
    assert variants("") == {"PLUS": "+"}
