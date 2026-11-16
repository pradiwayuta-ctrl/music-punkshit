import os
import json
import html
import asyncio
import urllib.parse
import urllib.request
from datetime import datetime
from pyrogram import Client

from config import MUSIC_VAULT_ID, CACHE_DIR, logger
from state import cleanup_old_cache

assistant_ref: Client | None = None
_tg_bot_lock = asyncio.Lock()


def init_finder(assistant: Client):
    global assistant_ref
    assistant_ref = assistant


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "Live / ∞"
    sec = int(seconds)
    mins, rem = divmod(sec, 60)
    hrs, mins = divmod(mins, 60)
    return f"{hrs}:{mins:02d}:{rem:02d}" if hrs else f"{mins}:{rem:02d}"


def _extract_audio_meta(msg, fallback_title: str) -> tuple[bool, str, int | None]:
    media_obj = msg.audio or msg.document or msg.voice
    if not media_obj:
        return False, fallback_title, None

    if msg.document and not msg.audio:
        fname = (msg.document.file_name or "").lower()
        mime = (msg.document.mime_type or "").lower()
        if not (mime.startswith("audio/") or fname.endswith((".mp3", ".m4a", ".flac", ".ogg", ".wav", ".opus"))):
            return False, fallback_title, None

    raw_title = (
        getattr(msg.audio, "title", None)
        or getattr(msg.audio, "file_name", None)
        or getattr(msg.document, "file_name", None)
        or (msg.caption.splitlines()[0] if msg.caption else None)
        or fallback_title
    )
    performer = getattr(msg.audio, "performer", None)
    display_title = (
        f"{raw_title} - {performer}"
        if (performer and performer.lower() not in raw_title.lower())
        else raw_title
    )
    for ext in (".mp3", ".m4a", ".flac", ".ogg", ".wav", ".opus"):
        if display_title.lower().endswith(ext):
            display_title = display_title[:-len(ext)]

    duration_sec = getattr(msg.audio, "duration", None) or getattr(msg.voice, "duration", None)
    return True, display_title, duration_sec


async def _search_music_vault(query: str) -> dict | None:
    if not MUSIC_VAULT_ID or not assistant_ref:
        return None
    try:
        async for msg in assistant_ref.search_messages(chat_id=MUSIC_VAULT_ID, query=query, limit=10):
            is_audio, display_title, duration_sec = _extract_audio_meta(msg, query)
            if not is_audio:
                continue

            dest_path = os.path.join(CACHE_DIR, f"vault_{msg.id}_{int(datetime.utcnow().timestamp())}")
            downloaded_path = await assistant_ref.download_media(msg, file_name=dest_path)
            if downloaded_path and os.path.exists(downloaded_path):
                cleanup_old_cache(keep_path=downloaded_path)
                return {
                    "title": display_title,
                    "stream_url": downloaded_path,
                    "webpage_url": msg.link or "",
                    "duration": format_duration(duration_sec),
                    "source_label": "Music Vault",
                }
    except Exception as e:
        logger.warning("Gagal mencari di MUSIC_VAULT_ID (%s): %s", MUSIC_VAULT_ID, e)
    return None


async def _fetch_via_tg_music_bot(query: str) -> dict | None:
    if not assistant_ref:
        return None
    music_bots = ["vkmusic_bot", "SpotSeekBot"]
    async with _tg_bot_lock:
        for bot_usn in music_bots:
            try:
                last_id = 0
                async for m in assistant_ref.get_chat_history(bot_usn, limit=1):
                    last_id = m.id

                await assistant_ref.send_message(bot_usn, query)
                target_audio_msg = None
                clicked = False

                for _ in range(12):
                    await asyncio.sleep(1.0)
                    recent = []
                    async for m in assistant_ref.get_chat_history(bot_usn, limit=6):
                        if m.id <= last_id:
                            break
                        if not m.outgoing:
                            recent.append(m)

                    for m in recent:
                        is_audio, _, _ = _extract_audio_meta(m, query)
                        if is_audio:
                            target_audio_msg = m
                            break
                    if target_audio_msg:
                        break

                    if not clicked:
                        for m in recent:
                            if m.reply_markup and getattr(m.reply_markup, "inline_keyboard", None):
                                for row in m.reply_markup.inline_keyboard:
                                    btn = row[0] if row else None
                                    if btn and getattr(btn, "callback_data", None):
                                        try:
                                            await assistant_ref.request_callback_answer(
                                                chat_id=bot_usn,
                                                message_id=m.id,
                                                callback_data=btn.callback_data,
                                            )
                                            clicked = True
                                            break
                                        except Exception:
                                            pass
                                    if clicked:
                                        break

                if not target_audio_msg:
                    continue

                is_audio, display_title, duration_sec = _extract_audio_meta(target_audio_msg, query)
                if not is_audio:
                    continue

                archived_link = ""
                if MUSIC_VAULT_ID:
                    try:
                        copied = await assistant_ref.copy_message(
                            chat_id=MUSIC_VAULT_ID,
                            from_chat_id=bot_usn,
                            message_id=target_audio_msg.id,
                        )
                        archived_link = getattr(copied, "link", "") or ""
                    except Exception:
                        pass

                dest_path = os.path.join(CACHE_DIR, f"tgbot_{target_audio_msg.id}_{int(datetime.utcnow().timestamp())}")
                downloaded_path = await assistant_ref.download_media(target_audio_msg, file_name=dest_path)
                if downloaded_path and os.path.exists(downloaded_path):
                    cleanup_old_cache(keep_path=downloaded_path)
                    return {
                        "title": display_title,
                        "stream_url": downloaded_path,
                        "webpage_url": archived_link,
                        "duration": format_duration(duration_sec),
                        "source_label": "Music Vault",
                    }
            except Exception as e:
                logger.warning("Gagal ambil dari @%s: %s", bot_usn, e)
                continue
    return None


def _search_jiosaavn_sync(query: str) -> dict | None:
    encoded = urllib.parse.quote_plus(query)
    for api_url in (
        f"https://saavn.dev/api/search/songs?query={encoded}&limit=1",
        f"https://jiosaavn-api-privatecvc2.vercel.app/search/songs?query={encoded}&limit=1",
    ):
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            results = ((data or {}).get("data") or {}).get("results") or []
            if not results:
                continue
            song = results[0]
            dl_urls = song.get("downloadUrl") or []
            best_url = dl_urls[-1].get("url") or dl_urls[-1].get("link") if dl_urls else None
            if not best_url:
                continue
            artists = ", ".join(
                a.get("name", "") for a in ((song.get("artists") or {}).get("primary") or []) if a.get("name")
            )
            title = html.unescape(song.get("name") or query)
            return {
                "title": f"{title} - {artists}" if artists else title,
                "stream_url": best_url,
                "webpage_url": song.get("url") or "",
                "duration": format_duration(song.get("duration")),
                "source_label": "Streaming",
            }
        except Exception:
            continue
    return None


async def resolve_audio_track(query: str) -> dict | None:
    vault_res = await _search_music_vault(query)
    if vault_res:
        return vault_res

    tg_bot_res = await _fetch_via_tg_music_bot(query)
    if tg_bot_res:
        return tg_bot_res

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _search_jiosaavn_sync, query)
