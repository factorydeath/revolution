from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

ENV_PREFIX = "YANDEXCAPTCHA_"

_SOLVER_FIELDS = (
    "lang",
    "test",
    "webview",
    "metadata",
    "user_agent",
    "profile",
    "randomize",
    "proxy",
    "timeout",
    "retries",
    "retry_backoff",
    "min_delay",
    "max_delay",
    "pow_workers",
    "connector_limit",
    "verify_ssl",
    "endpoint",
)

_BOOL_FIELDS = frozenset({"test", "webview", "verify_ssl", "randomize"})
_INT_FIELDS = frozenset({"retries", "pow_workers", "connector_limit", "concurrency", "count"})
_FLOAT_FIELDS = frozenset({"timeout", "retry_backoff", "min_delay", "max_delay"})


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if name in _INT_FIELDS:
        return int(value)
    if name in _FLOAT_FIELDS:
        return float(value)
    return str(value)


@dataclass(slots=True)
class Settings:
    sitekey: str | None = None
    host: str | None = None
    href: str | None = None
    url: str | None = None

    lang: str | None = None
    test: bool | None = None
    webview: bool | None = None
    metadata: str | None = None
    user_agent: str | None = None
    profile: str | None = None
    randomize: bool | None = None
    proxy: str | None = None
    proxy_file: str | None = None
    proxy_strategy: str | None = None
    cipher: str | None = None
    timeout: float | None = None
    retries: int | None = None
    retry_backoff: float | None = None
    min_delay: float | None = None
    max_delay: float | None = None
    pow_workers: int | None = None
    connector_limit: int | None = None
    verify_ssl: bool | None = None
    endpoint: str | None = None

    concurrency: int | None = None
    count: int | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Settings:
        known = {f.name for f in fields(cls)}
        clean = {
            key: _coerce(key, value)
            for key, value in data.items()
            if key in known and value is not None
        }
        return cls(**clean)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        environ = environ if environ is not None else os.environ
        data: dict[str, Any] = {}
        for key, value in environ.items():
            if not key.startswith(ENV_PREFIX):
                continue
            name = key[len(ENV_PREFIX):].lower()
            data[name] = value
        return cls.from_mapping(data)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> Settings:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return cls.from_mapping(data)

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        merged = cls.from_env(environ)
        if path:
            merged = merged.overlay(cls.from_file(path))
        return merged

    def _overlay(self, other: Settings) -> Settings:
        updates = {f.name: getattr(other, f.name) for f in fields(self) if getattr(other, f.name) is not None}
        return replace(self, **updates)

    def merge(self, **overrides: Any) -> Settings:
        clean = {k: v for k, v in overrides.items() if v is not None}
        known = {f.name for f in fields(self)}
        unknown = set(clean) - known
        if unknown:
            raise TypeError(f"unknown settings: {', '.join(sorted(unknown))}")
        return replace(self, **clean)

    def overlay(self, other: Settings) -> Settings:
        return self._overlay(other)

    def solver_kwargs(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in _SOLVER_FIELDS:
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out

    def target_kwargs(self) -> dict[str, Any]:
        out = {
            "sitekey": self.sitekey,
            "host": self.host,
            "href": self.href,
            "url": self.url,
        }
        return {k: v for k, v in out.items() if v is not None}

    def has_target(self) -> bool:
        return bool(self.url or (self.sitekey and self.host))

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def __repr__(self) -> str:
        shown = self.to_dict()
        if "sitekey" in shown:
            shown["sitekey"] = shown["sitekey"][:12] + "…"
        if "proxy" in shown:
            shown["proxy"] = "***"
        body = ", ".join(f"{k}={v!r}" for k, v in shown.items())
        return f"Settings({body})"
