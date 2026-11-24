from __future__ import annotations

import logging

from .api import (
    SyncSolver,
    resolve_target,
    solve,
    solve_detailed,
    solve_detailed_sync,
    solve_many_for_url,
    solve_sync,
)
from .cipher import (
    DEFAULT_SPEC,
    SPEC_A,
    SPEC_B,
    SPECS,
    CipherSpec,
    resolve_spec,
)
from .discover import (
    DiscoveryError,
    SiteKeyInfo,
    discover,
    discover_many,
    discover_sync,
    find_sitekey,
    host_of,
    normalize_url,
)
from .exceptions import (
    CaptchaRejectedError,
    CaptchaTaskError,
    ChallengeRequiredError,
    ProtocolError,
    TransportError,
    YandexCaptchaError,
)
from .fingerprint import (
    FIELD_INDEX,
    PGREED_BLOB,
    PGREED_VERSION,
    REFERENCE_RDATA,
    Profile,
    build_rdata,
    decode_rdata,
)
from .models import CaptchaTask, PowChallenge, SolveResult
from .pow import PowSolution, PowSolver
from .proxy import (
    Proxy,
    ProxyHealth,
    ProxyPool,
    SocksConnector,
    detect_proxy,
    load_proxies,
    parse_proxy,
    probe_proxy,
    resolve_pool,
)
from .solver import DEFAULT_ENDPOINT, DEFAULT_UA, YandexCaptchaSolver, solve_many

__version__ = "2.1.0"

__all__ = [
    "solve",
    "solve_sync",
    "solve_detailed",
    "solve_detailed_sync",
    "solve_many",
    "solve_many_for_url",
    "SyncSolver",
    "resolve_target",
    "YandexCaptchaSolver",
    "DEFAULT_ENDPOINT",
    "DEFAULT_UA",
    "discover",
    "discover_sync",
    "discover_many",
    "find_sitekey",
    "host_of",
    "normalize_url",
    "DiscoveryError",
    "SiteKeyInfo",
    "Proxy",
    "ProxyPool",
    "ProxyHealth",
    "SocksConnector",
    "parse_proxy",
    "load_proxies",
    "resolve_pool",
    "detect_proxy",
    "probe_proxy",
    "CipherSpec",
    "SPECS",
    "SPEC_A",
    "SPEC_B",
    "DEFAULT_SPEC",
    "resolve_spec",
    "Profile",
    "build_rdata",
    "decode_rdata",
    "REFERENCE_RDATA",
    "FIELD_INDEX",
    "PGREED_BLOB",
    "PGREED_VERSION",
    "CaptchaTask",
    "PowChallenge",
    "PowSolution",
    "PowSolver",
    "SolveResult",
    "YandexCaptchaError",
    "CaptchaRejectedError",
    "CaptchaTaskError",
    "ChallengeRequiredError",
    "ProtocolError",
    "TransportError",
    "__version__",
]


def enable_logging(level: int | str = logging.INFO) -> None:
    logger = logging.getLogger("yandexcaptcha")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level)
