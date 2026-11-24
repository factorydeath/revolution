from __future__ import annotations


import asyncio


import attack as prepare_mod


def test_prepare_tokens_collects_provider_output(monkeypatch):
    async def fake(session):
        return {"CHALLENGE": "abc"}

    monkeypatch.setitem(prepare_mod.PREPARERS, "fake", fake)
    tokens = asyncio.run(prepare_mod.prepare_tokens(object(), {"fake"}))
    assert tokens == {"fake": {"CHALLENGE": "abc"}}


def test_prepare_tokens_skips_failed_provider(monkeypatch):
    async def boom(session):
        raise RuntimeError("boom")

    monkeypatch.setitem(prepare_mod.PREPARERS, "boom", boom)
    tokens = asyncio.run(prepare_mod.prepare_tokens(object(), {"boom", "missing"}))
    assert tokens == {}

def test_loverepublic_token_is_dynamic(monkeypatch, tmp_path):
    token_file = tmp_path / "loverepublic.token"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.delenv("LOVEREPUBLIC_TOKEN", raising=False)
    monkeypatch.setenv("LOVEREPUBLIC_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(prepare_mod, "_lr_token_cache", ("", 0.0))
    token = asyncio.run(prepare_mod._discover_loverepublic_token(object()))
    assert token == "file-token"


def test_loverepublic_env_token_wins(monkeypatch):
    monkeypatch.setenv("LOVEREPUBLIC_TOKEN", "env-token")
    monkeypatch.setenv("LOVEREPUBLIC_TOKEN_FILE", "/nonexistent")
    monkeypatch.setattr(prepare_mod, "_lr_token_cache", ("", 0.0))
    assert asyncio.run(prepare_mod._discover_loverepublic_token(object())) == "env-token"


def test_loverepublic_invalidates_stale_token(monkeypatch):
    monkeypatch.setattr(prepare_mod, "_lr_token_cache", ("old", 10**12))
    prepare_mod._invalidate_loverepublic_token()
    assert prepare_mod._lr_token_cache == ("", 0.0)
