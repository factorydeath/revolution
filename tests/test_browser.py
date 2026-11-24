from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

import attack as attack_mod
import browser
from browser import (
    SERVICES,
    BrowserAttack,
    BrowserService,
    dial,
    proxy_settings,
    silpo,
)

def test_dial_adds_plus():
    assert dial("380671234567") == "+380671234567"
    assert dial("+380671234567") == "+380671234567"

def test_proxy_settings():
    assert proxy_settings("") is None
    assert proxy_settings("http://1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}
    assert proxy_settings("socks5h://1.2.3.4:1080") == {"server": "socks5://1.2.3.4:1080"}

def test_registry_has_silpo():
    assert "silpo" in SERVICES
    assert SERVICES["silpo"] is silpo
    assert callable(SERVICES["silpo"])

class FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

class FakeService(BrowserService):
    def __init__(self, name: str = "fake", confirmed: bool = True, error: Exception | None = None):
        self.name = name
        self.confirmed = confirmed
        self.error = error
        self.calls = 0

    async def send(self, page, phone: str) -> bool:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.confirmed

class FakeControl:
    def __init__(self) -> None:
        self.requests: list[tuple[int, str, bool]] = []
        self.errors: list[str] = []
        self.rate_available = True

    @property
    def stopped(self) -> bool:
        return False

    @property
    def deadline(self) -> float:
        return 1e18

    async def acquire_rate(self) -> bool:
        return self.rate_available

    async def pause(self, seconds: float) -> None:
        await asyncio.sleep(0)

    def note_request(self, status, service="", *, accepted=False) -> None:
        self.requests.append((status, service, accepted))

    def note_error(self, kind: str) -> None:
        self.errors.append(kind)

class FakeBrowserAttack(BrowserAttack):
    async def _ensure_browser(self) -> bool:
        return True

    async def _new_page(self, index: int = 0):
        return FakePage()

    async def _close_browser(self) -> None:
        return None

async def test_attack_runs_and_stops_cleanly():
    service_obj = FakeService()
    channel = FakeBrowserAttack(
        "380671234567", 30, services=[service_obj], workers=2, delay=0.01
    )
    channel.start()
    await asyncio.sleep(0.15)
    assert channel.running
    channel.stop()
    await asyncio.wait_for(channel.wait(), timeout=3)

    assert not channel.running
    assert service_obj.calls >= 2
    snap = channel.snapshot()
    assert snap["sent"] == service_obj.calls
    assert snap["ok"] == snap["sent"]
    assert snap["unconfirmed"] == 0
    assert snap["err"] == 0

async def test_unconfirmed_is_counted():
    service_obj = FakeService(confirmed=False)
    channel = FakeBrowserAttack("380671234567", 30, services=[service_obj], workers=1, delay=0.01)
    channel.start()
    await asyncio.sleep(0.1)
    await channel.aclose()
    snap = channel.snapshot()
    assert snap["sent"] >= 1
    assert snap["ok"] == 0
    assert snap["unconfirmed"] == snap["sent"]

async def test_service_error_is_counted():
    service_obj = FakeService(error=RuntimeError("boom"))
    channel = FakeBrowserAttack("380671234567", 30, services=[service_obj], workers=1, delay=0.01)
    channel.start()
    await asyncio.sleep(0.1)
    await channel.aclose()
    snap = channel.snapshot()
    assert snap["sent"] == 0
    assert snap["err"] >= 1
    assert snap["failures"]

async def test_stop_before_launch_is_safe():
    channel = FakeBrowserAttack("380671234567", 30, services=[FakeService()], workers=1)
    channel.start()
    channel.stop()
    await asyncio.wait_for(channel.wait(), timeout=2)
    assert channel.snapshot()["sent"] == 0

async def test_deadline_finishes_attack():
    service_obj = FakeService()
    channel = FakeBrowserAttack("380671234567", 1, services=[service_obj], workers=1, delay=0.01)
    channel.start(deadline=asyncio.get_running_loop().time() + 0.2)
    await asyncio.wait_for(channel.wait(), timeout=3)
    assert not channel.running
    assert service_obj.calls >= 1

async def test_launch_failure_does_not_crash():
    class DeadBrowser(FakeBrowserAttack):
        async def _ensure_browser(self) -> bool:
            return False

    channel = DeadBrowser("380671234567", 5, services=[FakeService()], workers=1)
    channel.start()
    await asyncio.wait_for(channel.wait(), timeout=3)
    assert channel.snapshot()["sent"] == 0

async def test_control_channel_gets_results():
    service_obj = FakeService()
    channel = FakeControl()
    attack_obj = FakeBrowserAttack(
        "380671234567", 30, services=[service_obj], workers=1, control=channel, delay=0.01
    )
    attack_obj.start()
    await asyncio.sleep(0.1)
    await attack_obj.aclose()
    assert channel.requests
    status, name, accepted = channel.requests[0]
    assert (status, name, accepted) == (200, "fake", True)

async def test_rate_limit_stops_workers():
    channel = FakeControl()
    channel.rate_available = False
    attack_obj = FakeBrowserAttack(
        "380671234567", 30, services=[FakeService()], workers=1, control=channel, delay=0.01
    )
    attack_obj.start()
    await asyncio.wait_for(attack_obj.wait(), timeout=2)
    assert attack_obj.snapshot()["sent"] == 0

async def test_service_as_function():
    calls = []

    async def custom_service(page, phone):
        calls.append((page, phone))
        return True

    channel = FakeBrowserAttack("380671234567", 30, services=[custom_service], workers=1, delay=0.01)
    channel.start()
    await asyncio.sleep(0.05)
    await channel.aclose()
    assert len(calls) >= 1
    assert calls[0][1] == "+380671234567"
    assert channel.snapshot()["ok"] >= 1

class RecordingAttack(FakeBrowserAttack):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.launches = 0
        self.closes = 0
        self.events: list[str] = []
        self.active = 0
        self.max_active = 0

    async def _ensure_browser(self) -> bool:
        self.launches += 1
        self.events.append("open")
        return True

    async def _close_browser(self) -> None:
        self.closes += 1
        self.events.append("close")


def _named_service(channel, name: str, delay: float = 0.02, error: Exception | None = None):
    async def flow(page, phone):
        channel.active += 1
        channel.max_active = max(channel.max_active, channel.active)
        channel.events.append(f"start:{name}")
        try:
            await asyncio.sleep(delay)
            if error is not None:
                raise error
            return True
        finally:
            channel.active -= 1
            channel.events.append(f"end:{name}")

    flow.name = name
    return flow


async def _run_until(channel, predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("условие не выполнено")
        await asyncio.sleep(0.01)
    channel.stop()
    await asyncio.wait_for(channel.wait(), timeout=2)


async def test_services_are_processed_in_single_browser_until_restart_threshold():
    channel = RecordingAttack("380671234567", 30, services=[], workers=2, delay=0.01)
    channel.MAX_SERVICES_PER_BROWSER = 4
    channel.services = [
        _named_service(channel, name, delay=0.01) for name in ("a", "b", "c", "d")
    ]
    channel.start()
    await _run_until(
        channel,
        lambda: {"start:a", "start:b", "start:c", "start:d"}.issubset(set(channel.events)),
    )

    starts = [event for event in channel.events if event.startswith("start:")]
    assert len(starts) >= 4
    assert channel.max_active >= 1


async def test_one_service_error_does_not_block_pair_or_next_batch():
    channel = RecordingAttack("380671234567", 30, services=[], workers=2, delay=0.01)
    channel.services = [
        _named_service(channel, "a", delay=0.01, error=RuntimeError("boom")),
        _named_service(channel, "b", delay=0.01),
        _named_service(channel, "c", delay=0.01),
        _named_service(channel, "d", delay=0.01),
    ]
    channel.start()
    await _run_until(
        channel,
        lambda: {"start:a", "start:b", "start:c", "start:d"}.issubset(set(channel.events)),
    )

    snapshot = channel.snapshot()
    assert snapshot["err"] >= 1
    assert snapshot["ok"] >= 2
    assert snapshot["sent"] >= 2


async def test_worker_one_keeps_pair_sequential():
    channel = RecordingAttack("380671234567", 30, services=[], workers=1, delay=0.01)
    channel.services = [
        _named_service(channel, name, delay=0.01) for name in ("a", "b")
    ]
    channel.start()
    await _run_until(channel, lambda: "end:b" in channel.events)
    assert channel.max_active == 1


async def test_close_browser_releases_pages_contexts_and_playwright():
    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class Playwright:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    page, context, browser, playwright = Resource(), Resource(), Resource(), Playwright()
    channel = BrowserAttack("380671234567", 30, services=[FakeService()])
    channel._pages.add(page)
    channel._contexts[0] = context
    channel._browser = browser
    channel._playwright = playwright

    await channel._close_browser()

    assert page.closed and context.closed and browser.closed and playwright.stopped
    assert not channel._pages
    assert not channel._contexts
    assert channel._browser is None
    assert channel._playwright is None

@pytest.fixture
async def manager():
    mgr = attack_mod.AttackManager([], proxy="", timeout=3, delay_ms=0, max_attacks=2, max_workers=2)
    yield mgr
    await mgr.shutdown()

async def test_attack_starts_and_stops_browser_channel(manager, monkeypatch):
    events: list[str] = []

    class StubBrowser:
        def __init__(self, phone, duration, **kwargs):
            events.append("init")

        def start(self, deadline=None):
            events.append("start")
            return self

        def stop(self, by_user=True):
            events.append("stop")

        async def wait(self):
            events.append("wait")

        async def aclose(self):
            events.append("aclose")

    monkeypatch.setattr("browser.BrowserAttack", StubBrowser)

    async def ok(_request):
        from aiohttp import web
        return web.Response(text="ok")

    from aiohttp import web
    app = web.Application()
    app.router.add_post("/ok", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    manager.templates = [
        attack_mod.Template(
            name="ok",
            country="RU",
            method="POST",
            url=f"http://127.0.0.1:{port}/ok",
            body="phone={{RAW}}",
            has_phone=True,
        )
    ]
    attack_obj = await manager.start(
        "79161234567", duration=2, workers=1, emulation=True, emul_workers=1
    )
    await asyncio.sleep(0.2)
    attack_obj.stop()
    await asyncio.wait_for(attack_obj.wait(), timeout=3)

    assert events[0] == "init"
    assert "start" in events
    assert "stop" in events
    assert "wait" in events
    assert "aclose" in events
    assert attack_obj.snapshot()["emulation"] is True
    await runner.cleanup()

def _has_chromium() -> bool:
    return pathlib.Path(pathlib.Path.home() / ".cache" / "ms-playwright").is_dir()

_PAGE = """<html><body>
  <form onsubmit="return false">
    <input type="tel" id="phone">
    <button id="go" onclick="send()">Увійти</button>
  </form>
  <div id="done" style="display:none">ok</div>
  <script>
    async function send() {
      await fetch('/otp', {method: 'POST', headers: {'Content-Type': 'application/json'},
                           body: JSON.stringify({phone: document.getElementById('phone').value})});
      document.getElementById('done').style.display = 'block';
    }
  </script>
</body></html>"""

class LocalForm(BrowserService):
    name = "local"
    inputs = ("input[name=missing]", "input[type=tel]")
    submits = ('button:has-text("Увійти")',)

    async def send(self, page, phone: str) -> bool:
        await page.goto(self.url, wait_until="domcontentloaded")
        from browser import first_visible

        field = await first_visible(page, self.inputs, timeout=10.0)
        if field is None:
            raise RuntimeError("поле не найдено")
        await field.fill(phone)
        button = await first_visible(page, self.submits, timeout=5.0)
        await button.click()
        await page.wait_for_timeout(800)
        return await first_visible(page, ("#done",), timeout=3.0) is not None

@pytest.mark.skipif(not _has_chromium(), reason="chromium не установлен")
async def test_silpo_like_flow_with_real_browser():
    from aiohttp import web

    hits: list[str] = []

    async def login(_request):
        return web.Response(text=_PAGE, content_type="text/html")

    async def otp(request):
        hits.append(await request.text())
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/login", login)
    app.router.add_post("/otp", otp)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    service_obj = LocalForm()
    service_obj.url = f"http://127.0.0.1:{port}/login"
    attack_obj = BrowserAttack(
        "380671234567", 15, services=[service_obj], workers=1, timeout=20.0, delay=0.2
    )
    attack_obj.start()
    for _ in range(60):
        await asyncio.sleep(0.5)
        if hits:
            break
    await asyncio.sleep(2.5)
    await attack_obj.aclose()
    await runner.cleanup()

    assert hits, "форма не отправлена"
    assert json.loads(hits[0]) == {"phone": "+380671234567"}
    snap = attack_obj.snapshot()
    assert snap["ok"] >= 1


def test_browser_attack_filters_services_by_country():
    ua_attack = BrowserAttack("380671234567", 10)
    ua_names = [getattr(s, "name", s.__name__) for s in ua_attack.services]
    assert "silpo" in ua_names
    for s in ua_attack.services:
        assert browser.SERVICE_COUNTRIES.get(getattr(s, "name", s.__name__)) == "UA"

    ru_attack = BrowserAttack("79161234567", 10)
    ru_names = [getattr(s, "name", s.__name__) for s in ru_attack.services]
    assert "silpo" not in ru_names
    assert "tbank" in ru_names
    assert "t2" in ru_names
    for s in ru_attack.services:
        assert browser.SERVICE_COUNTRIES.get(getattr(s, "name", s.__name__)) == "RU"


async def test_t2_flow(monkeypatch):
    calls = []

    class FakeLocator:
        def __init__(self, name):
            self.name = name

        @property
        def first(self):
            return self

        async def click(self, force=False):
            calls.append(f"click:{self.name}")

    class FakeKeyboard:
        async def type(self, text, delay=0):
            calls.append(f"type:{text}")

        async def press(self, key):
            calls.append(f"press:{key}")

    class FakePage:
        def __init__(self):
            self.keyboard = FakeKeyboard()

        async def goto(self, url, **kwargs):
            calls.append(f"goto:{url}")

        async def wait_for_timeout(self, ms):
            calls.append(f"timeout:{ms}")

        def get_by_text(self, text):
            return FakeLocator(f"text:{text}")

    page = FakePage()
    ok = await browser.t2(page, "+79161234567")
    assert ok is True
    assert "goto:https://t2.ru/" in calls
    assert "click:text:Войти" in calls
    assert "type:9161234567" in calls
    assert "press:Enter" in calls
