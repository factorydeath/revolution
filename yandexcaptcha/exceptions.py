from __future__ import annotations

from typing import Any

class YandexCaptchaError(Exception):
    pass

class CaptchaTaskError(YandexCaptchaError):
    pass

class CaptchaRejectedError(YandexCaptchaError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}

class ChallengeRequiredError(YandexCaptchaError):
    def __init__(self, challenge: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        kind = challenge.get("type", "unknown")
        super().__init__(f"interactive challenge required: {kind}")
        self.challenge = challenge
        self.payload = payload or {}

    @property
    def kind(self) -> str:
        return str(self.challenge.get("type", "unknown"))

    @property
    def image(self) -> str | None:
        image = self.challenge.get("image") or self.challenge.get("d")
        return image if isinstance(image, str) and image else None

class ProtocolError(YandexCaptchaError):
    pass

class TransportError(YandexCaptchaError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
