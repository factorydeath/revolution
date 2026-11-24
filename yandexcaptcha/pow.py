from __future__ import annotations

import asyncio
import hashlib
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class PowSolution:
    nonce: str
    elapsed_ms: int
    prefix: str
    complexity: int

def _meets_complexity(digest: bytes, bits: int) -> bool:
    whole, rest = divmod(bits, 8)
    if any(digest[:whole]):
        return False
    if rest:
        return digest[whole] >> (8 - rest) == 0
    return True

def _encode_nonce() -> bytes:
    stamp = int(time.time() * 1000).to_bytes(8, "big")
    return stamp + os.urandom(8)

def _search(prefix: bytes, complexity: int) -> tuple[bytes, int]:
    started = time.perf_counter()
    hasher = hashlib.sha256
    while True:
        nonce = _encode_nonce()
        if _meets_complexity(hasher(prefix + nonce).digest(), complexity):
            return nonce, int((time.perf_counter() - started) * 1000)

class PowSolver:
    def __init__(self, workers: int | None = None, inline_bits: int = 16) -> None:
        self.workers = max(1, workers or (os.cpu_count() or 1))
        self.inline_bits = inline_bits
        self._pool: ProcessPoolExecutor | None = None

    def _get_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=self.workers)
        return self._pool

    async def solve(self, prefix_hex: str, complexity: int) -> PowSolution:
        prefix = bytes.fromhex(prefix_hex)
        complexity = max(0, int(complexity))
        loop = asyncio.get_running_loop()

        if complexity <= self.inline_bits or self.workers == 1:
            nonce, elapsed = await loop.run_in_executor(None, _search, prefix, complexity)
        else:
            pool = self._get_pool()
            tasks = [
                loop.run_in_executor(pool, _search, prefix, complexity)
                for _ in range(self.workers)
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            nonce, elapsed = next(iter(done)).result()

        return PowSolution(
            nonce=nonce.hex(),
            elapsed_ms=elapsed,
            prefix=prefix_hex,
            complexity=complexity,
        )

    async def aclose(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await asyncio.get_running_loop().run_in_executor(None, pool.shutdown, True)

    async def __aenter__(self) -> PowSolver:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

def solve_sync(prefix_hex: str, complexity: int) -> PowSolution:
    prefix = bytes.fromhex(prefix_hex)
    nonce, elapsed = _search(prefix, int(complexity))
    return PowSolution(nonce.hex(), elapsed, prefix_hex, int(complexity))
