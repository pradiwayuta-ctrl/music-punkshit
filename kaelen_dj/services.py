import os
import glob
import time
import json
import asyncio
import html
import urllib.parse
import urllib.request
from datetime import datetime
from pyrogram import Client
from pyrogram.errors import UserAlreadyParticipant
from pyrogram.raw import functions, types as raw_types
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality

from config import MUSIC_VAULT_ID, CACHE_DIR, logger
from state import get_group_state, is_group_allowed, _call_id_to_chat

assistant: Client | None = None
call_py: PyTgCalls | None = None
_tg_bot_lock = asyncio.Lock()
_assistant_user_id = 0

def init_services(client: Client, call: PyTgCalls, assistant_uid: int):
    global assistant, call_py, _assistant_user_id
    assistant = client
    call_py = call
    _assistant_user_id = assistant_uid

async def ensure_assistant_in_group(chat_id: int, bot_client) -> bool:
    if not is_group_allowed(chat_id) or not assistant:
        return False
    try:
        await assistant.get_chat_member(chat_id, _assistant_user_id)
        return True
    except Exception:
        pass

    try:
        invite = await bot_client.create_chat_invite_link(chat_id=chat_id, member_limit=1)
        await assistant.join_chat(invite.invite_link)
        try:
            await bot_client.revoke_chat_invite_link(chat_id=chat_id, invite_link=invite.invite_link)
        except Exception:
            pass
        await asyncio.sleep(1.0)
        return True
    except UserAlreadyParticipant:
        return True
    except Exception as e:
        logger.warning("Asisten gagal auto-join grup %s: %s", chat_id, e)
        return False

def cleanup_old_cache(keep_path: str | None = None):
    try:
        from state import _groups
        active_paths = {
            os.path.abspath(st.current_track.stream_url)
            for st in _groups.values()
            if st.current_track and os.path.exists(st.current_track.stream_url)
        }
        for st in _groups.values():
            for t in st.queue:
                if os.path.exists(t.stream_url):
                    active_paths.add(os.path.abspath(t.stream_url))

        files = sorted(glob.glob(os.path.join(CACHE_DIR, "*")), key=os.path.getmtime)
        for f in files[:-5]:
            abs_f = os.path.abspath(f)
            if (keep_path and abs_f == os.path.abspath(keep_path)) or abs_f in active_paths:
                continue
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception:
        pass

def extract_audio_meta(msg, fallback_title: str) -> tuple[bool, str, int | None]:
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
    display_title = f"{raw_title} - {performer}" if (performer and performer.lower() not in raw_title.lower()) else raw_title
    for ext in (".mp3", ".m4a", ".flac", ".ogg", ".wav", ".opus"):
        if display_title.lower().endswith(ext):
            display_title = display_title[:-len(ext)]

    duration_sec = getattr(msg.audio, "duration", None) or getattr(msg.voice, "duration", None)
    return True, display_title, duration_sec

async def search_music_vault(query: str) -> dict | None:
    if not MUSIC_VAULT_ID or not assistant:
        return None
    try:
        async for msg in assistant.search_messages(chat_id=MUSIC_VAULT_ID, query=query, limit=10):
            is_audio, display_title, duration_sec = extract_audio_meta(msg, query)
            if not is_audio:
                continue
            dest_path = os.path.join(CACHE_DIR, f"vault_{msg.id}_{int(datetime.utcnow().timestamp())}")
            downloaded_path = await assistant.download_media(msg, file_name=dest_path)
            if downloaded_path and os.path.exists(downloaded_path):
                cleanup_old_cache(keep_path=downloaded_path)
                return {
                    "title": display_title,
                    "stream_url": downloaded_path,
                    "webpage_url": msg.link or "",
                    "duration": "Live / ∞" if not duration_sec else f"{duration_sec//60}:{duration_sec%60:02d}",
                    "source_label": "Music Vault",
                }
    except Exception as e:
        logger.warning("Gagal mencari di Vault: %s", e)
    return None

def search_jiosaavn_sync(query: str) -> dict | None:
    encoded = urllib.parse.quote_plus(query)
    api_url = f"https://saavn.dev/api/search/songs?query={encoded}&limit=1"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        results = ((data or {}).get("data") or {}).get("results") or []
        if not results:
            return None
        song = results[0]
        dl_urls = song.get("downloadUrl") or []
        best_url = dl_urls[-1].get("url") if dl_urls else None
        if not best_url:
            return None
        title = html.unescape(song.get("name") or query)
        return {
            "title": title,
            "stream_url": best_url,
            "webpage_url": song.get("url") or "",
            "duration": "Live / ∞",
            "source_label": "Streaming",
        }
    except Exception:
        return None

async def resolve_audio_track(query: str) -> dict | None:
    vault = await search_music_vault(query)
    if vault:
        return vault
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, search_jiosaavn_sync, query)

async def ensure_voice_chat_open(chat_id: int, bot_client) -> any:
    if not assistant:
        return None
    try:
        await ensure_assistant_in_group(chat_id, bot_client)
        peer = await assistant.resolve_peer(chat_id)
        full = await assistant.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full.full_chat, "call", None)
        if not call:
            await assistant.invoke(
                functions.phone.CreateGroupCall(
                    peer=peer,
                    random_id=assistant.rnd_id(),
                    title="Kaelen's Lounge",
                )
            )
            await asyncio.sleep(1.0)
            full = await assistant.invoke(functions.channels.GetFullChannel(channel=peer))
            call = getattr(full.full_chat, "call", None)
        return call
    except Exception as e:
        logger.error("Gagal membuka OS di grup %s: %s", chat_id, e)
        return None
