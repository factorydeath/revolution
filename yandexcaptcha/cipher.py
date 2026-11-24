from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import lru_cache

MASK32 = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class CipherSpec:
    name: str
    state: tuple[int, ...]
    rounds: int
    pgrd: str

    def with_counter(self, counter: int) -> list[int]:
        initial = list(self.state)
        initial[12] = counter & MASK32
        return initial

    def __repr__(self) -> str:
        return f"<CipherSpec {self.name} rounds={self.rounds}>"


SPEC_A = CipherSpec(
    name="6.3.1-a",
    state=(
        1868719733,
        1935892852,
        1696622450,
        1701143585,
        1619873276,
        3821723123,
        3404663977,
        1042053282,
        724060814,
        303469071,
        2715807040,
        89914004,
        0,
        344738823,
        521800798,
        3887828770,
    ),
    rounds=18,
    pgrd=(
        "9JlbyT+h/ZOPECg4KvtYLjkDJctbDFpu4pJtnKUoXPPNkbjY4/1OL7mDE8mvE/s/AM0EdKNZuiEkaqBODDz"
        "2T/qV3+Kw3+D0TpNKue4b1UC+f7oha0UTVAHxhT/rQG35M9V3exII3cidXwfSr5FaGHiauKQOhzfYYVxjk"
        "t8qYRnkhHJ3SLPZHfSQCHLUShI7wl63ybvT/dMI0vEE6QHvFhEFRwY="
    ),
)

SPEC_B = CipherSpec(
    name="6.3.1-b",
    state=(
        1868719733,
        1935892852,
        1696622450,
        1701143585,
        4040270657,
        2464022648,
        2259056123,
        1096188770,
        2423482019,
        1004785936,
        1775057117,
        1422308524,
        0,
        3411645079,
        2619148037,
        1910102532,
    ),
    rounds=13,
    pgrd=(
        "hjfsEDYUFChMv4wluVs6Xtrw87E2gDkOPmy98O191iOTgIBA2SmLy/JmX1GJScAs4LjZVhTKtdDR4xkPEajm7"
        "ukvBCrLFJO0WBohUppalqZLYQ8hFKCQRwfm/gcscvhpzw5M5W2a9XrcTEifP/cMo+YiGA3vizlg1bDPETmOO"
        "gpctOD+WH//W/83p/JG4iw42Edmeq9km783/pyCTnTEhLkEk04="
    ),
)

SPECS: tuple[CipherSpec, ...] = (SPEC_A, SPEC_B)
DEFAULT_SPEC = SPECS[0]

SPEC_BY_NAME = {spec.name: spec for spec in SPECS}
SPEC_BY_NAME["a"] = SPEC_A
SPEC_BY_NAME["b"] = SPEC_B
SPEC_BY_NAME["legacy"] = SPEC_A
SPEC_BY_NAME["current"] = SPEC_B


def resolve_spec(spec: CipherSpec | str | None) -> CipherSpec:
    if spec is None:
        return DEFAULT_SPEC
    if isinstance(spec, CipherSpec):
        return spec
    try:
        return SPEC_BY_NAME[spec]
    except KeyError:
        choices = ", ".join(spec.name for spec in SPECS)
        raise ValueError(f"unknown cipher spec {spec!r} (known: {choices})") from None


def _rotl(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (32 - shift))) & MASK32


def _quarter_round(s: list[int], a: int, b: int, c: int, d: int) -> None:
    s[a] = (s[a] + s[b]) & MASK32
    s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & MASK32
    s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & MASK32
    s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & MASK32
    s[b] = _rotl(s[b] ^ s[c], 7)


def _permute(state: list[int], rounds: int) -> None:
    for _ in range(rounds):
        _quarter_round(state, 0, 4, 8, 12)
        _quarter_round(state, 1, 5, 9, 13)
        _quarter_round(state, 2, 6, 10, 14)
        _quarter_round(state, 3, 7, 11, 15)
        _quarter_round(state, 0, 5, 10, 15)
        _quarter_round(state, 1, 6, 11, 12)
        _quarter_round(state, 2, 7, 8, 13)
        _quarter_round(state, 3, 4, 9, 14)


@lru_cache(maxsize=4096)
def _words(spec: CipherSpec, counter: int) -> tuple[int, ...]:
    initial = spec.with_counter(counter)
    state = list(initial)
    _permute(state, spec.rounds)
    return tuple((state[i] + initial[i]) & MASK32 for i in range(16))


def keystream(
    spec: CipherSpec | str | int | None = None, counter: int | None = None
) -> bytes:
    if counter is None:
        counter = int(spec)
        actual = DEFAULT_SPEC
    else:
        actual = resolve_spec(spec)
    out = bytearray(64)
    for i, word in enumerate(_words(actual, counter)):
        out[4 * i : 4 * i + 4] = word.to_bytes(4, "little")
    return bytes(out)


def encrypt_value(
    text: str, counter: int, spec: CipherSpec | str | None = None
) -> str:
    actual = resolve_spec(spec)
    words = _words(actual, counter)
    payload = bytearray(2 * len(text))
    for i, char in enumerate(text):
        word = words[(i // 2) % 16]
        half = (word >> 16 if i % 2 else word & 0xFFFF) & 0xFFFF
        code = ord(char) & 0xFFFF
        payload[2 * i] = (code & 0xFF) ^ (half & 0xFF)
        payload[2 * i + 1] = ((code >> 8) & 0xFF) ^ (half >> 8)
    return f"{base64.b64encode(bytes(payload)).decode()};{counter}"


def decrypt_value(value: str, spec: CipherSpec | str | None = None) -> str:
    try:
        encoded, counter_text = value.rsplit(";", 1)
        counter = int(counter_text)
    except ValueError as exc:
        raise ValueError(f"not a PGreed field: {value!r}") from exc
    raw = base64.b64decode(encoded)
    if len(raw) % 2:
        raise ValueError(f"odd ciphertext length for field counter {counter}")
    if spec is None:
        best: tuple[float, str] | None = None
        for candidate in SPECS:
            text = _decrypt_raw(raw, counter, candidate)
            score = plausibility(text)
            if best is None or score > best[0]:
                best = (score, text)
        assert best is not None
        return best[1]
    return _decrypt_raw(raw, counter, resolve_spec(spec))


def _decrypt_raw(raw: bytes, counter: int, spec: CipherSpec) -> str:
    words = _words(spec, counter)
    chars = []
    for i in range(len(raw) // 2):
        word = words[(i // 2) % 16]
        half = (word >> 16 if i % 2 else word & 0xFFFF) & 0xFFFF
        chars.append(chr((raw[2 * i] ^ (half & 0xFF)) | ((raw[2 * i + 1] ^ (half >> 8)) << 8)))
    return "".join(chars)


def plausibility(text: str) -> float:
    if not text:
        return 1.0
    good = 0
    for char in text:
        code = ord(char)
        if 0x20 <= code <= 0x7E or code in (0x9, 0xA, 0xD) or 0xA0 <= code <= 0x2FFF:
            good += 1
    return good / len(text)
