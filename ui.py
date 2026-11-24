from __future__ import annotations

import asyncio
import html
import json
import logging
import pathlib
import re
from typing import Any

from aiogram import Bot
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichMessage,
    InputRichMessageMedia,
    Message,
)

logger = logging.getLogger("revolution")

EMOJI: dict[str, tuple[str, str]] = {
    "sword": ("5453991094435997597", "⚔️"),
    "blade": ("5454172148782359440", "🗡️"),
    "bolt": ("5843553939672274145", "⚡️"),
    "crown": ("5807868868886009920", "👑"),
    "star": ("5958376256788502078", "⭐️"),
    "spark": ("5222108309795908493", "✨"),
    "fire": ("6008118472066732010", "🔥"),
    "burst": ("5219901967916084166", "💥"),
    "shield": ("5926783847453692661", "🛡"),
    "ok": ("5776375003280838798", "✅"),
    "no": ("5778527486270770928", "❌"),
    "warn": ("5881702736843511327", "⚠️"),
    "bang": ("5879813604068298387", "❗️"),
    "gem": ("5807465992363710697", "💎"),
    "phone": ("5897567714674741148", "📞"),
    "device": ("5985833664884250583", "📱"),
    "user": ("5771887475421090729", "👤"),
    "home": ("5967822972931542886", "🏠"),
    "play": ("5264919878082509254", "▶️"),
    "pause": ("5359543311897998264", "⏸"),
    "ru": ("5449408995691341691", "🇷🇺"),
    "ua": ("5447309366568953338", "🇺🇦"),
    "target": ("5350460637182993292", "🎯"),
    "lock": ("5832546462478635761", "🔒"),
    "key": ("6005570495603282482", "🔑"),
    "chart": ("5877485980901971030", "📊"),
    "ban": ("5872829476143894491", "🚫"),
    "doc": ("5839323457015256759", "📄"),
    "gear": ("5341715473882955310", "⚙️"),
    "info": ("5879785854284599288", "ℹ️"),
    "back": ("5875082500023258804", "⬅️"),
    "moon": ("5377377257356537351", "🌙"),
    "flower": ("5967574255670399788", "🌸"),
    "black": ("5449692618151695997", "🖤"),
    "white": ("5451714942157724312", "🤍"),
    "siren": ("5395695537687123235", "🚨"),
    "bell": ("5909201569898827582", "🔔"),
    "mute": ("5909123362839335003", "🔕"),
    "coin": ("5811989245761426317", "💰"),
    "eye": ("5960714428394507968", "👁"),
    "door": ("5877341274863832725", "🚪"),
    "pin": ("5908961403917570106", "📌"),
    "clock": ("5985616167740379273", "⏰"),
    "calendar": ("5985616167740379273", "📅"),
    "hourglass": ("5451646226975955576", "⌛️"),
    "wait": ("5451732530048802485", "⏳"),
    "candle": ("5451882707875276247", "🕯"),
    "skull": ("5357592447557848986", "💀"),
    "green": ("5339135753316222622", "🟢"),
    "dot": ("5994324703559290598", "⚪️"),
}

ICONS = {
    "order": "sword",
    "profile": "user",
    "protect": "shield",
    "history": "doc",
    "support": "bell",
    "channel": "bell",
    "vip": "crown",
    "back": "back",
    "accept": "ok",
    "pay": "star",
    "stop": "no",
    "add": "lock",
    "remove": "ban",
    "users": "user",
    "grant": "crown",
    "revoke": "no",
    "ban": "ban",
    "unban": "ok",
    "allow": "key",
    "unallow": "ban",
    "access": "door",
    "search": "eye",
    "stats": "chart",
    "broadcast": "siren",
    "close": "no",
    "limits": "gear",
    "reset": "hourglass",
    "panel": "home",
    "send": "play",
    "cancel": "no",
}

PRIMARY = ButtonStyle.PRIMARY
DANGER = ButtonStyle.DANGER
SUCCESS = ButtonStyle.SUCCESS

EFFECT_HEART = "5159385139981059251"
EFFECT_FIRE = "5104841245755180586"

def e(name: str) -> str:
    pair = EMOJI.get(name)
    if not pair:
        return ""
    emoji_id, fallback = pair
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def icon(name: str | None) -> str | None:
    if not name:
        return None
    pair = EMOJI.get(ICONS.get(name, name))
    return pair[0] if pair else None

def esc(value: object) -> str:
    return html.escape(str(value), quote=False)

def btn(
    text: str,
    callback_data: str | None = None,
    *,
    icon_name: str | None = None,
    style: ButtonStyle | None = None,
    url: str | None = None,
    pay: bool | None = None,
) -> InlineKeyboardButton:
    kwargs: dict = {}
    emoji_id = icon(icon_name)
    if emoji_id:
        kwargs["icon_custom_emoji_id"] = emoji_id
    if style is not None:
        kwargs["style"] = style
    if url is not None:
        kwargs["url"] = url
    if pay:
        kwargs["pay"] = True
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    return InlineKeyboardButton(text=text, **kwargs)

def h(text: str, level: int = 2) -> str:
    return f"<h{level}>{text}</h{level}>"

def quote(body: str) -> str:
    return f"<blockquote>{body}</blockquote>"

def details(title: str, body: str, *, open_: bool = False) -> str:
    flag = " open" if open_ else ""
    return f"<details{flag}><summary>{title}</summary>{body}</details>"

def _cap(text: str) -> str:
    if not text:
        return text
    clean = re.sub(r"<[^>]+>", "", text).lstrip()
    if not clean or not clean[0].isalpha():
        return text
    in_tag = False
    for i, ch in enumerate(text):
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if in_tag or ch.isspace():
            continue
        if ch.isalpha():
            return text[:i] + ch.upper() + text[i + 1:]
        break
    return text

def fields(pairs: list[tuple[str, str]]) -> str:
    lines = []
    for label, value in pairs:
        if value is None or value == "":
            continue
        lines.append(f"<b>{_cap(label)}</b>\n{value}")
    return "\n\n".join(lines)

def card(
    title: str,
    body: str,
    *,
    footer: str | None = None,
    level: int = 2,
    section: str | None = None,
) -> str:
    parts = [h(title, level)]
    if body:
        parts.append(quote(body))
    if footer:
        parts.append(f"<footer>{footer}</footer>")
    return "\n".join(parts)

def row(*cells: str) -> str:
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"

def sheet(pairs: list[tuple[str, str]]) -> str:
    rows = [[_cap(label), value] for label, value in pairs if value not in (None, "")]
    if not rows:
        return ""
    return table([], rows)

def table(headers: list[str], rows: list[list[str]]) -> str:
    has_headers = any(bool(cell and str(cell).strip()) for cell in headers) if headers else False
    head = (
        "<tr>" + "".join(f"<th>{_cap(cell)}</th>" for cell in headers) + "</tr>"
        if has_headers
        else ""
    )
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
        for cells in rows
    )
    return f"<table bordered compact>{head}{body}</table>"

def bullets(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"

def _media_descriptor(path: pathlib.Path) -> tuple[str, str, type]:
    suffix = path.suffix.lower()
    if suffix in (".mp4", ".mov", ".m4v", ".webm"):
        return ("video", "tg://video?id=hero", InputMediaVideo)
    if suffix in (".gif",):
        return ("video", "tg://video?id=hero", InputMediaAnimation)

    try:
        with open(path, "rb") as f:
            header = f.read(16)
        if header.startswith(b"GIF8"):
            return ("video", "tg://video?id=hero", InputMediaAnimation)
        if b"ftyp" in header:
            return ("video", "tg://video?id=hero", InputMediaVideo)
    except Exception:
        pass

    return ("img", "tg://photo?id=hero", InputMediaPhoto)

_CACHE_FILE = pathlib.Path(__file__).resolve().parent / "pic" / ".media_cache.json"
_MEDIA_CACHE: dict[str, dict[str, Any]] = {}
_MAX_MEDIA_CACHE = 512

def _load_media_cache() -> None:
    global _MEDIA_CACHE
    if _CACHE_FILE.exists():
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                _MEDIA_CACHE = json.load(f)
        except Exception as exc:
            logger.debug("cannot load media cache: %r", exc)
            _MEDIA_CACHE = {}

def _save_media_cache() -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_MEDIA_CACHE, f, indent=2)
        tmp.replace(_CACHE_FILE)
    except Exception as exc:
        logger.debug("cannot save media cache: %r", exc)

def _prune_media_cache() -> None:
    """Очистка старейших записей при переполнении кеша"""
    global _MEDIA_CACHE
    if len(_MEDIA_CACHE) <= _MAX_MEDIA_CACHE:
        return
    # Удаляем треть старейших записей
    to_remove = list(_MEDIA_CACHE.keys())[:len(_MEDIA_CACHE) - _MAX_MEDIA_CACHE]
    for key in to_remove:
        _MEDIA_CACHE.pop(key, None)

def get_cached_media_source(
    path: pathlib.Path,
) -> tuple[str, str, type, str | FSInputFile]:
    tag, uri, media_cls = _media_descriptor(path)
    try:
        st = path.stat()
        mtime_ns = st.st_mtime_ns
        size = st.st_size
    except Exception:
        return (tag, uri, media_cls, FSInputFile(path))

    key = str(path.resolve())
    cached = _MEDIA_CACHE.get(key)
    if (
        cached
        and cached.get("mtime_ns") == mtime_ns
        and cached.get("size") == size
        and cached.get("file_id")
    ):
        return (tag, uri, media_cls, str(cached["file_id"]))

    return (tag, uri, media_cls, FSInputFile(path))

def record_cached_media(
    path: pathlib.Path | str | None, sent: Message | None
) -> None:
    if not path or not sent:
        return
    resolved_path = pathlib.Path(path).resolve()
    key = str(resolved_path)
    try:
        st = resolved_path.stat()
        mtime_ns = st.st_mtime_ns
        size = st.st_size
    except Exception:
        return

    file_id = None
    media_type = "photo"
    if getattr(sent, "video", None):
        file_id = sent.video.file_id
        media_type = "video"
    elif getattr(sent, "animation", None):
        file_id = sent.animation.file_id
        media_type = "animation"
    elif getattr(sent, "photo", None):
        file_id = sent.photo[-1].file_id
        media_type = "photo"
    elif getattr(sent, "document", None):
        file_id = sent.document.file_id
        media_type = "document"

    if file_id:
        _prune_media_cache()  # Очистка перед добавлением новой записи
        _MEDIA_CACHE[key] = {
            "file_id": file_id,
            "mtime_ns": mtime_ns,
            "size": size,
            "media_type": media_type,
        }
        _save_media_cache()
        logger.info("cached media file_id for %s (type=%s)", resolved_path.name, media_type)

def invalidate_media_cache(path: pathlib.Path | str | None) -> None:
    if not path:
        return
    key = str(pathlib.Path(path).resolve())
    if key in _MEDIA_CACHE:
        del _MEDIA_CACHE[key]
        _save_media_cache()

_load_media_cache()

def rich(
    html_text: str, photo: pathlib.Path | str | None = None
) -> InputRichMessage:
    media = None
    body = html_text
    path = pathlib.Path(photo) if photo else None
    if path is not None and path.exists():
        tag, uri, media_cls, media_source = get_cached_media_source(path)
        media = [
            InputRichMessageMedia(
                id="hero", media=media_cls(media=media_source)
            )
        ]
        body = f'<figure><{tag} src="{uri}"/></figure>\n' + body
    return InputRichMessage(html=body, media=media)

def _benign(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "message is not modified" in text
        or "message to edit not found" in text
        or "message to delete not found" in text
    )

_last_sticker_ids: dict[int, int] = {}
_MAX_STICKER_CACHE = 2048

async def _send_sticker_clean(bot: Bot, chat_id: int, sticker_id: str) -> Message | None:
    old_id = _last_sticker_ids.pop(chat_id, None)
    if old_id:
        try:
            await bot.delete_message(chat_id, old_id)
        except Exception:
            pass
    
    # Очистка кеша при переполнении
    if len(_last_sticker_ids) >= _MAX_STICKER_CACHE:
        # Удаляем первую половину (старейшие записи)
        to_remove = list(_last_sticker_ids.keys())[:_MAX_STICKER_CACHE // 2]
        for key in to_remove:
            _last_sticker_ids.pop(key, None)
    
    try:
        st_msg = await bot.send_sticker(chat_id, sticker=sticker_id)
        if st_msg:
            _last_sticker_ids[chat_id] = st_msg.message_id
        return st_msg
    except Exception as exc:
        logger.warning("send_sticker failed for %s: %r", chat_id, exc)
        return None

def resolve_sticker(
    sticker: str | None = None, photo: pathlib.Path | str | None = None
) -> str | None:
    if sticker:
        return sticker
    if photo:
        try:
            import config
            name = pathlib.Path(photo).name
            return config.PHOTO_TO_STICKER.get(name)
        except Exception:
            pass
    return None

def _format_table(match: re.Match) -> str:
    table_html = match.group(0)
    rows = re.findall(r"<tr>(.*?)</tr>", table_html, flags=re.DOTALL)
    out = []
    headers: list[str] = []
    data_rows: list[list[str]] = []

    for r in rows:
        if "<th" in r and "<td" not in r:
            th_cells = [
                re.sub(r"</?th>", "", c).strip()
                for c in re.findall(r"<th>(.*?)</th>", r, flags=re.DOTALL)
            ]
            if any(th_cells):
                headers = th_cells
            continue
        cells = [
            re.sub(r"</?t[hd]>", "", c).strip()
            for c in re.findall(r"<t[hd]>(.*?)</t[hd]>", r, flags=re.DOTALL)
        ]
        if cells:
            data_rows.append(cells)

    if headers and len(headers) > 2:
        out.append(" | ".join(f"<b>{h}</b>" for h in headers if h))

    for cells in data_rows:
        if len(cells) == 2:
            if cells[0]:
                out.append(f"▫️ <b>{cells[0]}:</b> {cells[1]}")
        elif len(cells) == 3:
            if cells[0]:
                out.append(f"▫️ <b>{cells[0]}:</b> {cells[1]} ➔ <b>{cells[2]}</b>")
            else:
                out.append(f"▫️ {cells[1]} ➔ <b>{cells[2]}</b>")
        elif len(cells) > 3:
            out.append(" | ".join(c for c in cells if c))
        elif cells:
            out.append(cells[0])

    return "\n".join(out)

def _to_plain_html(text: str) -> str:
    text = re.sub(r"<figure>.*?</figure>\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"<h\d>(.*?)</h\d>", r"<b>\1</b>\n\n", text)
    text = re.sub(r"<summary>(.*?)</summary>", r"<b>\1</b>\n", text)
    text = re.sub(r"</?details[^>]*>", "", text)
    text = re.sub(r"<footer>(.*?)</footer>", r"\n<i>\1</i>", text, flags=re.DOTALL)
    text = re.sub(r"<table[^>]*>.*?</table>", _format_table, text, flags=re.DOTALL)
    text = re.sub(r"</?t[hd]>", " ", text)
    return text.strip()

async def show(
    message: Message,
    html_text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    sticker: str | None = None,
    photo: pathlib.Path | str | None = None,
    new: bool = False,
    message_effect_id: str | None = None,
) -> Message | None:
    stk = resolve_sticker(sticker, photo)
    payload = rich(html_text, photo=None)

    if stk is not None:
        try:
            await message.delete()
        except Exception:
            pass
        await _send_sticker_clean(message.bot, message.chat.id, stk)
        return await _send(
            message.bot,
            message.chat.id,
            payload,
            reply_markup,
            message_effect_id=message_effect_id,
        )

    if not new:
        try:
            edited = await message.edit_text(
                rich_message=payload,
                reply_markup=reply_markup,
            )
            return edited if isinstance(edited, Message) else message
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return message

    sent = await _send(
        message.bot,
        message.chat.id,
        payload,
        reply_markup,
        message_effect_id=message_effect_id,
    )
    if sent is not None and not new:
        try:
            await message.delete()
        except Exception:
            pass
    return sent

async def say(
    bot: Bot,
    chat_id: int,
    html_text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    sticker: str | None = None,
    photo: pathlib.Path | str | None = None,
    message_effect_id: str | None = None,
) -> Message | None:
    stk = resolve_sticker(sticker, photo)
    payload = rich(html_text, photo=None)

    if stk is not None:
        await _send_sticker_clean(bot, chat_id, stk)

    return await _send(
        bot,
        chat_id,
        payload,
        reply_markup,
        message_effect_id=message_effect_id,
    )

async def _send(
    bot: Bot,
    chat_id: int,
    payload: InputRichMessage,
    reply_markup: InlineKeyboardMarkup | None,
    photo_path: pathlib.Path | None = None,
    message_effect_id: str | None = None,
) -> Message | None:
    for attempt in (1, 2):
        if payload.media:
            m = payload.media[0].media
            caption = _to_plain_html(payload.html or "")
            if caption and len(caption) > 1024:
                caption = caption[:1020] + "…"
            if isinstance(m, InputMediaVideo):
                try:
                    sent = await bot.send_video(
                        chat_id,
                        video=m.media,
                        caption=caption or None,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                        message_effect_id=message_effect_id,
                    )
                    record_cached_media(photo_path, sent)
                    return sent
                except TelegramRetryAfter as exc:
                    if attempt == 2:
                        return None
                    await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 1)
                    continue
                except Exception as exc:
                    if message_effect_id:
                        try:
                            sent = await bot.send_video(
                                chat_id,
                                video=m.media,
                                caption=caption or None,
                                reply_markup=reply_markup,
                                parse_mode="HTML",
                            )
                            record_cached_media(photo_path, sent)
                            return sent
                        except Exception:
                            pass
                    if isinstance(m.media, str) and photo_path and photo_path.exists():
                        logger.warning(
                            "cached file_id for %s failed: %r, retrying with upload",
                            photo_path.name,
                            exc,
                        )
                        invalidate_media_cache(photo_path)
                        try:
                            sent = await bot.send_video(
                                chat_id,
                                video=FSInputFile(photo_path),
                                caption=caption or None,
                                reply_markup=reply_markup,
                                parse_mode="HTML",
                                message_effect_id=message_effect_id,
                            )
                            record_cached_media(photo_path, sent)
                            return sent
                        except Exception as fb_exc:
                            logger.warning("send_video fallback failed: %r", fb_exc)
                            return None
                    logger.warning("send_video failed for %s: %r", chat_id, exc)
                    return None
            elif isinstance(m, InputMediaAnimation):
                try:
                    sent = await bot.send_animation(
                        chat_id,
                        animation=m.media,
                        caption=caption or None,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                        message_effect_id=message_effect_id,
                    )
                    record_cached_media(photo_path, sent)
                    return sent
                except TelegramRetryAfter as exc:
                    if attempt == 2:
                        return None
                    await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 1)
                    continue
                except Exception as exc:
                    if message_effect_id:
                        try:
                            sent = await bot.send_animation(
                                chat_id,
                                animation=m.media,
                                caption=caption or None,
                                reply_markup=reply_markup,
                                parse_mode="HTML",
                            )
                            record_cached_media(photo_path, sent)
                            return sent
                        except Exception:
                            pass
                    if isinstance(m.media, str) and photo_path and photo_path.exists():
                        logger.warning(
                            "cached file_id for %s failed: %r, retrying with upload",
                            photo_path.name,
                            exc,
                        )
                        invalidate_media_cache(photo_path)
                        try:
                            sent = await bot.send_animation(
                                chat_id,
                                animation=FSInputFile(photo_path),
                                caption=caption or None,
                                reply_markup=reply_markup,
                                parse_mode="HTML",
                                message_effect_id=message_effect_id,
                            )
                            record_cached_media(photo_path, sent)
                            return sent
                        except Exception as fb_exc:
                            logger.warning("send_animation fallback failed: %r", fb_exc)
                            return None
                    logger.warning("send_animation failed for %s: %r", chat_id, exc)
                    return None

        try:
            sent = await bot.send_rich_message(
                chat_id,
                payload,
                reply_markup=reply_markup,
                message_effect_id=message_effect_id,
            )
            if payload.media:
                record_cached_media(photo_path, sent)
            return sent
        except TelegramRetryAfter as exc:
            if attempt == 2:
                logger.warning("rich send rate-limited for %s", chat_id)
                return None
            await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 1)
        except Exception as exc:
            logger.warning("rich send failed for %s: %r, trying fallback", chat_id, exc)
            try:
                caption = _to_plain_html(payload.html or "")
                if payload.media:
                    m = payload.media[0].media
                    if isinstance(m, InputMediaPhoto):
                        photo_caption = (caption[:1020] + "…") if len(caption) > 1024 else caption
                        sent = await bot.send_photo(
                            chat_id,
                            photo=m.media,
                            caption=photo_caption or None,
                            reply_markup=reply_markup,
                            parse_mode="HTML",
                            message_effect_id=message_effect_id,
                        )
                        record_cached_media(photo_path, sent)
                        return sent
                return await bot.send_message(
                    chat_id,
                    text=caption or "-",
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    message_effect_id=message_effect_id,
                )
            except Exception as fb_exc:
                logger.warning("fallback send failed for %s: %r", chat_id, fb_exc)
                return None
    return None
