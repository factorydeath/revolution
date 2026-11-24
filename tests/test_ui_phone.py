from __future__ import annotations

import pytest

from main import (
    display_phone,
    is_fake_number,
    normalize_phone,
    phone_country,
    split_phone_candidates,
)

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+7 (916) 384-51-20", "79163845120"),
        ("8 916 384 51 20", "79163845120"),
        ("+380 (67) 392-03-82", "380673920382"),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected

@pytest.mark.parametrize("raw", ["", "123", "abcdef", "+1 555 0100", "0000000000"])
def test_normalize_invalid(raw):
    assert normalize_phone(raw) is None

def test_display_and_country():
    assert display_phone("79163845120").startswith("+7")
    assert display_phone("380673920382").startswith("+380")
    assert phone_country("79163845120") == "RU"
    assert phone_country("380673920382") == "UA"

@pytest.mark.parametrize(
    "phone,fake",
    [
        ("79999999999", True),
        ("79161234567", False),
        ("71234567890", True),
        ("70000000000", True),
    ],
)
def test_is_fake_number(phone, fake):
    assert is_fake_number(phone) is fake

def test_split_multiline_and_commas():
    raw = "+79163845120, 380673920382; 89163845120"
    assert split_phone_candidates(raw) == [
        "+79163845120",
        "380673920382",
        "89163845120",
    ]

def test_split_formatted_ru():
    assert split_phone_candidates("8 960 444 11 22") == ["8 960 444 11 22"]
    assert split_phone_candidates("79373801863 380387492389") == [
        "79373801863",
        "380387492389",
    ]

@pytest.mark.parametrize("separator", ["\n", ",", ";", ",\n"])
def test_split_two_ten_digit_ru_numbers(separator):
    raw = f"9161234567{separator}9167654321"
    assert split_phone_candidates(raw) == ["9161234567", "9167654321"]

def test_ui_rich_media_video_and_photo(tmp_path):
    import ui
    from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaAnimation

    mp4_file = tmp_path / "test.mp4"
    mp4_file.write_bytes(b"\x00\x00\x00 ftypisom")
    rich_video = ui.rich("<b>Video</b>", mp4_file)
    assert '<video src="tg://video?id=hero"/>' in rich_video.html
    assert isinstance(rich_video.media[0].media, InputMediaVideo)

    gif_file = tmp_path / "test.gif"
    gif_file.write_bytes(b"GIF89a\x01\x00")
    rich_gif = ui.rich("<b>GIF</b>", gif_file)
    assert '<video src="tg://video?id=hero"/>' in rich_gif.html
    assert isinstance(rich_gif.media[0].media, InputMediaAnimation)

    jpg_file = tmp_path / "test.jpg"
    jpg_file.write_bytes(b"\xff\xd8\xff\xe0")
    rich_jpg = ui.rich("<b>Photo</b>", jpg_file)
    assert '<img src="tg://photo?id=hero"/>' in rich_jpg.html
    assert isinstance(rich_jpg.media[0].media, InputMediaPhoto)

    plain = ui._to_plain_html('<figure><video src="tg://video?id=hero"/></figure>\n<h2>Header</h2>')
    assert "figure" not in plain
    assert "<b>Header</b>" in plain

    from types import SimpleNamespace
    from aiogram.types import FSInputFile

    assert isinstance(rich_video.media[0].media.media, FSInputFile)

    fake_sent = SimpleNamespace(video=SimpleNamespace(file_id="cached_file_id_123"))
    ui.record_cached_media(mp4_file, fake_sent)

    cached_rich = ui.rich("<b>Cached</b>", mp4_file)
    assert cached_rich.media[0].media.media == "cached_file_id_123"

    ui.invalidate_media_cache(mp4_file)
    uncached_rich = ui.rich("<b>Uncached</b>", mp4_file)
    assert isinstance(uncached_rich.media[0].media.media, FSInputFile)

def test_ui_cap_preserves_tags():
    import ui
    assert ui._cap("vip") == "Vip"
    assert ui._cap("Бан") == "Бан"
    assert ui._cap('<tg-emoji emoji-id="5807868868886009920">👑</tg-emoji> VIP') == '<tg-emoji emoji-id="5807868868886009920">👑</tg-emoji> VIP'
