from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class PowChallenge:
    prefix: str
    complexity: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PowChallenge:
        return cls(prefix=str(data["prefix"]), complexity=int(data.get("complexity", 0)))


@dataclass(slots=True)
class CaptchaTask:
    captcha_key: str
    captcha_type: str
    unique_key: str
    pow: PowChallenge
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SolveResult:
    token: str
    elapsed: float
    attempts: int
    task: CaptchaTask | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def decode_token(token: str) -> dict[str, str]:
        def parse(text: str) -> dict[str, str]:
            result: dict[str, str] = {}
            for chunk in text.split(";"):
                if "=" in chunk:
                    key, value = chunk.split("=", 1)
                    if key and key.isalnum():
                        result[key] = value
            return result

        pairs = parse(token)
        if "i" in pairs or "t" in pairs:
            return pairs

        try:
            decoded = base64.b64decode(token + "=" * (-len(token) % 4)).decode()
        except Exception:
            return pairs
        return parse(decoded)

    @property
    def claims(self) -> dict[str, str]:
        return self.decode_token(self.token)

    @property
    def ip(self) -> str | None:
        return self.claims.get("i")

    @property
    def issued_at(self) -> int | None:
        stamp = self.claims.get("t")
        return int(stamp) if stamp and stamp.isdigit() else None

    @property
    def age(self) -> float | None:
        issued = self.issued_at
        return None if issued is None else time.time() - issued

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "token": self.token,
            "elapsed_ms": round(self.elapsed * 1000),
            "attempts": self.attempts,
            "ip": self.ip,
            "issued_at": self.issued_at,
        }
        if self.task is not None:
            data["captcha_type"] = self.task.captcha_type
            data["unique_key"] = self.task.unique_key
            data["pow_complexity"] = self.task.pow.complexity
        return data

    def __str__(self) -> str:
        return self.token
