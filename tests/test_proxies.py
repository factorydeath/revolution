from __future__ import annotations

import pytest

from proxies import Proxy, ProxyPool, load_proxies, parse_proxy


def test_parse_plain_host_port():
    proxy = parse_proxy("1.2.3.4:8080")
    assert proxy is not None
    assert (proxy.scheme, proxy.host, proxy.port) == ("http", "1.2.3.4", 8080)
    assert proxy.username == "" and proxy.password == ""
    assert proxy.proxy_url == "http://1.2.3.4:8080"
    assert proxy.request_kwargs() == {"proxy": "http://1.2.3.4:8080"}


def test_parse_host_port_user_pass():
    proxy = parse_proxy("1.2.3.4:8080:user:pass")
    assert proxy is not None
    assert proxy.username == "user"
    assert proxy.password == "pass"
    assert proxy.auth is not None


def test_parse_url_with_credentials():
    proxy = parse_proxy("http://user:pass@1.2.3.4:8080")
    assert proxy is not None
    assert (proxy.scheme, proxy.host, proxy.port) == ("http", "1.2.3.4", 8080)
    assert (proxy.username, proxy.password) == ("user", "pass")
    kwargs = proxy.request_kwargs()
    assert kwargs["proxy"] == "http://user:pass@1.2.3.4:8080"
    assert kwargs["proxy_auth"].login == "user"
    assert kwargs["proxy_auth"].password == "pass"


def test_parse_socks():
    proxy = parse_proxy("socks5://user:pass@1.2.3.4:1080")
    assert proxy is not None
    assert proxy.is_socks
    assert proxy.request_kwargs() == {}
    proxy = parse_proxy("socks4://1.2.3.4:1080")
    assert proxy is not None and proxy.is_socks


def test_parse_rejects_garbage():
    for line in ("", "   ", "# comment", "1.2.3.4", "1.2.3.4:notaport",
                 "1.2.3.4:0", "1.2.3.4:99999", "a:b:c:d:e"):
        assert parse_proxy(line) is None


def test_load_proxies_dedupes_and_skips_bad(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text(
        "# header\n"
        "1.2.3.4:8080\n"
        "\n"
        "1.2.3.4:8080\n"
        "5.6.7.8:3128:user:pass\n"
        "broken-line\n",
        encoding="utf-8",
    )
    proxies = load_proxies(path)
    assert [p.label for p in proxies] == ["1.2.3.4:8080", "5.6.7.8:3128"]


def test_load_proxies_missing_file(tmp_path):
    assert load_proxies(tmp_path / "nope.txt") == []


def test_pool_pick_returns_members():
    entries = [Proxy("http", "1.1.1.1", 80), Proxy("http", "2.2.2.2", 80)]
    pool = ProxyPool(entries)
    picked = {pool.pick().label for _ in range(200)}
    assert picked == {"1.1.1.1:80", "2.2.2.2:80"}
    assert len(pool) == 2 and bool(pool)


def test_pool_cooldown_after_threshold():
    first = Proxy("http", "1.1.1.1", 80)
    second = Proxy("http", "2.2.2.2", 80)
    pool = ProxyPool([first, second], cooldown=60.0, threshold=2)

    pool.report(first, False)
    assert pool.stats() == {"total": 2, "cooling": 0, "alive": 2}
    pool.report(first, False)
    assert pool.stats() == {"total": 2, "cooling": 1, "alive": 1}
    assert [p.label for p in pool.healthy()] == ["2.2.2.2:80"]

    pool.report(first, True)
    assert pool.stats() == {"total": 2, "cooling": 0, "alive": 2}


def test_pool_success_resets_failure_counter():
    proxy = Proxy("http", "1.1.1.1", 80)
    pool = ProxyPool([proxy], threshold=2)
    pool.report(proxy, False)
    pool.report(proxy, True)
    pool.report(proxy, False)
    assert pool.stats()["cooling"] == 0
    pool.report(proxy, False)
    assert pool.stats()["cooling"] == 1


def test_pool_pick_returns_something_while_all_cooling():
    proxy = Proxy("http", "1.1.1.1", 80)
    pool = ProxyPool([proxy], threshold=1)
    pool.report(proxy, False)
    assert pool.pick() is proxy


def test_pool_empty():
    pool = ProxyPool([])
    assert pool.pick() is None
    assert pool.stats() == {"total": 0, "cooling": 0, "alive": 0}


async def test_pool_connection_http_and_direct():
    session = object()
    proxy = Proxy("http", "1.1.1.1", 8080, "u", "p")
    pool = ProxyPool([proxy], session)

    chosen, kwargs = await pool.connection(proxy)
    assert chosen is session
    assert kwargs["proxy"] == "http://u:p@1.1.1.1:8080"
    assert kwargs["proxy_auth"].login == "u"

    chosen, kwargs = await pool.connection(None)
    assert chosen is session and kwargs == {}


def test_proxy_from_unknown_scheme_falls_back_to_http():
    proxy = parse_proxy("https://1.2.3.4:8080")
    assert proxy is not None
    assert proxy.scheme == "http"
    assert proxy.url == "http://1.2.3.4:8080"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("10.0.0.1:1080:alice:secret", ("alice", "secret")),
        ("http://alice:secret@10.0.0.1:1080", ("alice", "secret")),
        ("10.0.0.1:1080", ("", "")),
    ],
)
def test_parse_credentials_variants(line, expected):
    proxy = parse_proxy(line)
    assert proxy is not None
    assert (proxy.username, proxy.password) == expected


def test_pool_warns_once_per_cooldown(caplog, monkeypatch):
    import logging

    import proxies as proxies_mod

    proxy = Proxy("http", "1.1.1.1", 80)
    pool = ProxyPool([proxy], cooldown=60.0, threshold=1)
    now = {"t": 1_000.0}
    monkeypatch.setattr(proxies_mod.time, "monotonic", lambda: now["t"])
    caplog.set_level(logging.WARNING, logger="noreply")

    pool.report(proxy, False)
    pool.report(proxy, False)
    pool.report(proxy, False)
    warnings = [r for r in caplog.records if "пауза" in r.getMessage()]
    assert len(warnings) == 1

    now["t"] += 61.0
    pool.report(proxy, False)
    warnings = [r for r in caplog.records if "пауза" in r.getMessage()]
    assert len(warnings) == 2


def test_load_proxies_json_dict(tmp_path):
    import json

    path = tmp_path / "proxies.json"
    data = {
        "RU": [
            "socks5://user:pass@1.1.1.1:1080",
            {"proxy": "http://1.1.1.2:8080"},
        ],
        "UA": [
            "socks5://2.2.2.1:1080",
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    proxies = load_proxies(path)
    assert len(proxies) == 3
    ru_proxies = [p for p in proxies if p.country == "RU"]
    ua_proxies = [p for p in proxies if p.country == "UA"]
    assert len(ru_proxies) == 2
    assert len(ua_proxies) == 1
    assert ru_proxies[0].label == "1.1.1.1:1080"
    assert ru_proxies[0].country == "RU"
    assert ua_proxies[0].label == "2.2.2.1:1080"
    assert ua_proxies[0].country == "UA"


def test_load_proxies_json_list(tmp_path):
    import json

    path = tmp_path / "proxies.json"
    data = [
        {"proxy": "socks5://1.1.1.1:1080", "country": "RU"},
        {"url": "http://2.2.2.2:8080", "country": "UA"},
    ]
    path.write_text(json.dumps(data), encoding="utf-8")

    proxies = load_proxies(path)
    assert len(proxies) == 2
    assert proxies[0].country == "RU"
    assert proxies[1].country == "UA"


def test_pool_pick_country_isolation():
    ru_proxy = Proxy("http", "1.1.1.1", 80, country="RU")
    ua_proxy = Proxy("http", "2.2.2.2", 80, country="UA")
    pool = ProxyPool([ru_proxy, ua_proxy])

    for _ in range(50):
        picked = pool.pick("UA")
        assert picked is ua_proxy
        assert picked.country == "UA"

    for _ in range(50):
        picked = pool.pick("RU")
        assert picked is ru_proxy
        assert picked.country == "RU"

    assert pool.pick("KZ") is None
    assert pool.pick("US") is None


def test_pool_pick_never_leaks_other_country_when_empty_or_cooling():
    ru_proxy = Proxy("http", "1.1.1.1", 80, country="RU")
    pool = ProxyPool([ru_proxy], cooldown=60.0, threshold=1)

    assert pool.pick("UA") is None

    ua_proxy = Proxy("http", "2.2.2.2", 80, country="UA")
    pool.update_entries([ru_proxy, ua_proxy])

    pool.report(ua_proxy, False)
    assert pool.healthy("UA") == []

    picked = pool.pick("UA")
    assert picked is ua_proxy


def test_pool_stats_by_country():
    ru_proxy = Proxy("http", "1.1.1.1", 80, country="RU")
    ua_proxy = Proxy("http", "2.2.2.2", 80, country="UA")
    pool = ProxyPool([ru_proxy, ua_proxy])

    stats = pool.stats()
    assert stats["total"] == 2
    assert "by_country" in stats
    assert stats["by_country"]["RU"]["total"] == 1
    assert stats["by_country"]["UA"]["total"] == 1
