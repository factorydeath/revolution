from __future__ import annotations

import base64
import json
import os
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .cipher import (
    DEFAULT_SPEC,
    SPECS,
    CipherSpec,
    decrypt_value,
    encrypt_value,
    resolve_spec,
)

REFERENCE_RDATA = "eyJhMSI6IkxGZUtRSmdEUUxMUndnPT07MCIsImEyIjoiL3FhM01VREdsQWN3QXYrOXlBbzdkUDUzTTIrdStBPT07MSIsImEzIjoiUGVKM0ZuUCs4WlY1ZzV6eDF2VlpiQT09OzIiLCJhNCI6InIvczlabVNBazRVYm9BPT07MyIsImE1IjoiK2Y3dlZ5Qi9VZFdlZFE9PTs0IiwiYTYiOiJRQzA9OzUiLCJhNyI6InVYMXFCNzRaU0FpMG93PT07NiIsImE4Ijoic01KWDR6ZkhFbWs9OzciLCJhOSI6IlVPSU9QNHcwaFNnV1FBPT07OCIsImIxIjoiRTUvSmMvS085b3c9OzkiLCJiMiI6IkkyanpSbS9RU1lhMnhRPT07MTAiLCJiMyI6IjJPK0g1V1Y4UjI3eEJnPT07MTEiLCJiNCI6IkR0Z2NHdFZFU1VjPTsxMiIsImI1IjoiQmFSQS9QQUZtWE0xWFE9PTsxMyIsImI2IjoiNTk0ckVUSDh4SGxmWGc9PTsxNCIsImI3IjoiMXhYN0Q4anFQS1V0a3c9PTsxNSIsImI4IjoiNEkrN1BqUCszSVM3V2c9PTsxNiIsImI5IjoiYmhSTkpxaHk3S3c9OzE3IiwiYzEiOiJGTHNKS3l1TDsxOCIsImMyIjoiKzdncDMrQm1oNzFTZFJ1OEFaTnAxMDR0OzE5IiwiYzMiOiJoTWZhaFRLMG15WWZObTlhVjliSytpbWs7MjAiLCJjNCI6Imh6OVBHbTd6UnFJPTsyMSIsImM1IjoiRWNuRUZGK3UyV0k9OzIyIiwiYzYiOiJXdldjU3c9PTsyMyIsImM3IjoiMWFSSi9RRmxuZkJZbFE9PTsyNCIsImM4IjoiQzBJPTsyNSIsImM5IjoiUjJaWXI1em5tQlk9OzI2IiwiZDEiOiJPamp5OWRrTk1nST07MjciLCJkMiI6IjBReW9pUT09OzI4IiwiZDMiOiJlc1RDdmU2K3hscWR3dz09OzI5IiwiZDQiOiJ2elF3cW0vMENxND07MzAiLCJkNSI6Im1OZjNEUWZ5Y1U2STBRPT07MzEiLCJkNyI6InlxNXVlSnZGdHRZPTszMiIsImQ4IjoiYzhMaXlqUm5OSHJSdEc5Z0RyZWxpL094WlR3YzRRVmxaVlE9OzMzIiwiZDkiOiJiM2NoS2xuNktMdz07MzQiLCJlMSI6IlZqcEk2elVwcHdTTUNRPT07MzUiLCJlMiI6InNWTC9QZEsxc0RNPTszNiIsImUzIjoiUVNNcUV6NHNmQ289OzM3IiwiZTQiOiJjSTgrWHQrTk9XVT07MzgiLCJlNSI6IjEyYVBKY0UvQXB0MEZBPT07MzkiLCJlNiI6IkVHR3BZWkFlbnFRPTs0MCIsImU3IjoiZk96OWt2dk9UTTg2V0E9PTs0MSIsImU4IjoiMHZxVWdEQ0pmWWc9OzQyIiwiZTkiOiI4NUVJcEdOL3F2OD07NDMiLCJmMSI6Ik0wSzhUTi8welFxU1B3PT07NDQiLCJmMiI6Im94dmxZWUNYdStZPTs0NSIsImYzIjoic0JZeHVPaERURUxidHc9PTs0NiIsImY0IjoiTkZkVHdtZzdPV1E9OzQ3IiwiZjUiOiJTMFVzZ3B4R2JjYng5dz09OzQ4IiwiZjYiOiJ1cmdpWmdxaXpnOVJCUT09OzQ5IiwiZjciOiJTTFRkaDAyR0ZFNHordz09OzUwIiwiZjgiOiJydXJjL2lRK3hxTzI4dz09OzUxIiwiZjkiOiJYdm15bXdZTHZPST07NTIiLCJnMSI6ImtPRXkzQnhESWllbnZnPT07NTMiLCJnMiI6IlBkTGlicUpRZHdLM0FBPT07NTQiLCJnMyI6Ilg2MjZLWVBCV29vPTs1NSIsImc0IjoiYmhWZ0FBUzljVzJMUXc9PTs1NiIsImc1Ijoid3BFb1VKSFhNWWs9OzU3IiwiZzYiOiI4eUIxS3kxRWNjND07NTgiLCJnNyI6IlZSWFN5d3pCSWw4PTs1OSIsImc4IjoiMnNyaDljTDIrbFE9OzYwIiwiZzkiOiJsSjkrZ216bGYrQT07NjEiLCJoMSI6ImNWSUpnZEx0YVp2bHZ3PT07NjIiLCJoMiI6IjZTb0M1OWMwM0N3ZlZRPT07NjMiLCJoMyI6IjJJNjNPUXFYaTZHYmxRPT07NjQiLCJoNCI6IkNqaEoxd1FyUmdUYm5BPT07NjUiLCJoNSI6Iko0d3hlWGc1WVhBPTs2NiIsImg2IjoiWUl1RlhOc0RLcUhLY0E9PTs2NyIsImg3IjoiY2FjdW55RFg4LzZVU3JXQlpBNDEranNFbHBidk1XSDlhODBSa3ROMkkwWU9OWDNoOzY4IiwiaDgiOiJVVW1Gcm5nUGVnK01Rdz09OzY5IiwiaDkiOiJkQ2RpcnBhNEJJVlBsQT09OzcwIiwiaTEiOiJQbE9WTi9pdWdpdz07NzEiLCJpMiI6InNGODNDbEpZRVpMMG1RPT07NzIiLCJpMyI6IldCcE1WQTBuL080RnVBPT07NzMiLCJpNCI6Im1ndk1NWkt4L2FnMTdBPT07NzQiLCJpNSI6IjViVy9tWllhZkFOMGtnPT07NzUiLCJpNiI6IjYzL0dXQT09Ozc2IiwiejEiOiJhV29XdG5ETGJOYnFQTGdwN0gzVGUvVVRTTCtqNHVNSXQ2UjZxMGI2NFZ2VlphNnZRWGJtWm9zZExyMFZXKyt0NlN6Q1owajBtRk1FWjRQUzB3SVdtUT09Ozc3IiwiejIiOiJIREErOUtwYisxYTBFWFZobkFJUDhPUldiQXpBUXNna24wakhjeXl5SUtuNEQxcnkxUThlY2R1dzhpRy9SOStYNEVaK0VveXA0bkdTeDlBcnYzbTZ2QT09Ozc4IiwieTIiOiJpWHJrR0tBSjEwWjVqQT09Ozc5IiwieTMiOiJNOHFSMTl5Tzk1ejdMUT09OzgwIiwieTYiOiJJT1FyUFpmcFBLeHJ0UT09OzgxIiwieTgiOiJHeEd5U0xXUVE3MWoxdz09OzgyIiwieDQiOiJKdXJkTW1paG02Tmtjdz09OzgzIiwiejMiOiJQcHUyczhrMUhkMmt4c3Q1Ozg0IiwiejQiOiJGNTdocEdpM2huU1BoZz09Ozg1IiwiejUiOiJhRXA2cU1qaEZObz07ODYiLCJ6NiI6Iit2YjhLUC9xNDFqMXBwdGI7ODciLCJ6NyI6InpLNnpiNzNjYTlqS25LNmE7ODgiLCJ6OCI6IllQSVZLYm5iSUZGUmxETEg7ODkiLCJ6OSI6Iit5UkpQK1hRNU1SYmZ5M2M7OTAiLCJ5MSI6ImJRbnZOZGpYWEdYTGNhNE87OTEiLCJ5NCI6IkUwZkVDVTNCUFl5YXRwcFg7OTIiLCJ5NSI6Im53MUxGajBhNU11OUpZcjE7OTMiLCJ5NyI6IjFYNXRyakJRbnB5WllnanI7OTQiLCJ5OSI6IktIR0Y0UFNhNWVUalRTSjE7OTUiLCJ5MTAiOiJjeXBvS0d5U0o4YW5McUliOzk2IiwieDEiOiJZWWlOanVJR1RkWEx3aWR4Ozk3IiwieDIiOiJTbFVtc2REc0FWc3FsMzVsOzk4IiwieDMiOiJJYW1nZnlDYzJhbmt3UUtOOzk5IiwieDUiOiJERURtY3F0WnNydGNESk90OzEwMCIsInYiOiI2LjMuMSIsInBncmR0IjoiNUFnSW5hV09meFhweWQyWFpOb0J4YlhpazZvPTsxMDEiLCJwZ3JkIjoiOUpsYnlUK2gvWk9QRUNnNEt2dFlMamtESmN0YkRGcHU0cEp0bktVb1hQUE5rYmpZNC8xT0w3bURFOG12RS9zL0FNMEVkS05adWlFa2FxQk9ERHoyVC9xVjMrS3czK0QwVHBOS3VlNGIxVUMrZjdvaGEwVVRWQUh4aFQvclFHMzVNOVYzZXhJSTNjaWRYd2ZTcjVGYUdIaWF1S1FPaHpmWVlWeGprdDhxWVJua2hISjNTTFBaSGZTUUNITFVTaEk3d2w2M3lidlQvZE1JMHZFRTZRSHZGaEVGUndZPSJ9"
PGREED_VERSION = "6.3.1"
PGREED_BLOB = DEFAULT_SPEC.pgrd
PLAIN_FIELDS = frozenset({"v", "pgrd"})
SKIP_INDEX = frozenset({"pgrd"})


def _decode_reference() -> dict[str, str]:
    raw = json.loads(base64.b64decode(REFERENCE_RDATA))
    values: dict[str, str] = {}
    for key, value in raw.items():
        if key in PLAIN_FIELDS:
            values[key] = value
        elif isinstance(value, str) and ";" in value:
            values[key] = decrypt_value(value)
        else:
            values[key] = str(value)
    return values


def _indices_from(rdata: str) -> dict[str, int]:
    raw = json.loads(base64.b64decode(rdata))
    return {
        key: int(value.rsplit(";", 1)[1])
        for key, value in raw.items()
        if key not in PLAIN_FIELDS
        and isinstance(value, str)
        and ";" in value
        and value.rsplit(";", 1)[1].isdigit()
    }


REFERENCE_VALUES: dict[str, str] = _decode_reference()
FIELD_INDEX: dict[str, int] = _indices_from(REFERENCE_RDATA)

SCREEN_SIZES: tuple[tuple[str, str], ...] = (
    ("1366x768", "1366x768"),
    ("1440x900", "1440x900"),
    ("1536x864", "1536x864"),
    ("1600x900", "1600x900"),
    ("1680x1050", "1680x1050"),
    ("1920x1080", "1920x1080"),
    ("1920x1200", "1920x1200"),
    ("2560x1440", "2560x1440"),
)


def detect_spec(rdata: str) -> CipherSpec:
    raw = json.loads(base64.b64decode(rdata))
    sample = [
        value
        for value in raw.values()
        if isinstance(value, str) and ";" in value
    ][:8]
    best: tuple[float, CipherSpec] | None = None
    for spec in SPECS:
        score = 0.0
        for value in sample:
            try:
                score += plausibility_of(decrypt_value(value, spec))
            except Exception:
                continue
        if best is None or score > best[0] + 1e-9:
            best = (score, spec)
    return best[1] if best else DEFAULT_SPEC


def plausibility_of(text: str) -> float:
    from .cipher import plausibility

    return plausibility(text)


@dataclass
class Profile:
    values: dict[str, str]
    indices: dict[str, int]
    order: list[str] = field(default_factory=list)
    spec: CipherSpec = DEFAULT_SPEC

    @classmethod
    def reference(cls, spec: CipherSpec | str | None = None) -> Profile:
        chosen = resolve_spec(spec)
        values = dict(REFERENCE_VALUES)
        values["pgrd"] = chosen.pgrd
        return cls(values, dict(FIELD_INDEX), list(REFERENCE_VALUES), chosen)

    @classmethod
    def from_rdata(
        cls, rdata: str, spec: CipherSpec | str | None = None
    ) -> Profile:
        chosen = resolve_spec(spec) if spec is not None else detect_spec(rdata)
        raw = json.loads(base64.b64decode(rdata))
        values: dict[str, str] = {}
        indices: dict[str, int] = {}
        for key, value in raw.items():
            if key in PLAIN_FIELDS:
                values[key] = value
                continue
            if not isinstance(value, str) or ";" not in value:
                values[key] = str(value)
                continue
            values[key] = decrypt_value(value, chosen)
            indices[key] = int(value.rsplit(";", 1)[1])
        return cls(values, indices, list(raw), chosen)

    @classmethod
    def from_json(cls, path: str | os.PathLike[str]) -> Profile:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if "rdata" in data:
            return cls.from_rdata(data["rdata"], data.get("spec"))
        spec = resolve_spec(data.get("spec")) if data.get("spec") else DEFAULT_SPEC
        return cls(
            values={k: str(v) for k, v in data["values"].items()},
            indices={k: int(v) for k, v in data["indices"].items()},
            order=list(data.get("order") or data["values"].keys()),
            spec=spec,
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> Profile:
        profile = cls.reference()
        missing = set(profile.values) - set(values)
        if missing:
            raise ValueError(f"profile is missing fields: {', '.join(sorted(missing))}")
        return profile.update(**values)

    def set(self, name: str, value: Any) -> Profile:
        if name not in self.values:
            raise KeyError(f"unknown fingerprint field: {name}")
        self.values[name] = str(value)
        return self

    def update(self, **values: Any) -> Profile:
        for key, value in values.items():
            self.set(key, value)
        return self

    def randomize(self, rng: random.Random | None = None, *, screen: bool = True) -> Profile:
        rng = rng or random.Random()
        if screen and "c2" in self.values and "c3" in self.values:
            width, height = rng.choice(SCREEN_SIZES)
            self.values["c2"] = width
            self.values["c3"] = height
        for name in ("z1", "z2"):
            if name in self.values:
                self.values[name] = os.urandom(16).hex()
        return self

    def to_rdata(self, timestamp: int | None = None) -> str:
        now = int(time.time()) if timestamp is None else int(timestamp)
        encoded: dict[str, str] = {}
        for name in self.order:
            value = self.values[name]
            if name == "pgrdt":
                encoded[name] = encrypt_value(str(now), self.indices[name], self.spec)
            elif name in PLAIN_FIELDS:
                encoded[name] = self.spec.pgrd if name == "pgrd" else value
            else:
                encoded[name] = encrypt_value(value, self.indices[name], self.spec)
        payload = json.dumps(encoded, separators=(",", ":")).encode()
        return base64.b64encode(payload).decode()

    def to_json(self, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        data = {
            "values": self.values,
            "indices": self.indices,
            "order": self.order,
            "version": self.values.get("v"),
            "spec": self.spec.name,
        }
        if path is not None:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        return data

    def as_text(self) -> dict[str, str]:
        return dict(self.values)


def build_rdata(
    profile: Profile | Mapping[str, str] | None = None,
    *,
    timestamp: int | None = None,
    spec: CipherSpec | str | None = None,
) -> str:
    if profile is None:
        return Profile.reference(spec).to_rdata(timestamp)
    if isinstance(profile, Profile):
        return profile.to_rdata(timestamp)
    result = Profile.from_mapping(profile)
    if spec is not None:
        result.spec = resolve_spec(spec)
        result.values["pgrd"] = result.spec.pgrd
    return result.to_rdata(timestamp)


def decode_rdata(
    rdata: str, spec: CipherSpec | str | None = None
) -> dict[str, str]:
    return Profile.from_rdata(rdata, spec).as_text()
