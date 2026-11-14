import os
import json
import html
import glob
import time
import asyncio
import logging
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from pyrogram import Client
from pyrogram.errors import UserAlreadyParticipant
from pyrogram.raw import functions, types as raw_types
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
)
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kaelen_dj_multigroup")

# --- KONFIGURASI ENVIRONMENT VARIABLES ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
OWNER_ID = int(os.environ["OWNER_ID"])


def _parse_allowed_groups() -> set[int]:
    raw = os.environ.get("ALLOWED_GROUPS", "")
    allowed: set[int] = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        token = part.strip()
        if token and token.lstrip("-").isdigit():
            allowed.add(int(token))
    return allowed


def _get_music_vault_id() -> int | str | None:
    raw = os.environ.get("MUSIC_VAULT_ID")
    if not raw:
        return None
    val = str(raw).strip()
    return int(val) if val.lstrip("-").isdigit() else val


ALLOWED_GROUPS: set[int] = _parse_allowed_groups()
MUSIC_VAULT_ID = _get_music_vault_id()
AUTO_DELETE_DELAY = 45
OS_MUTE_SWEEP_INTERVAL = 10
OS_IDLE_TIMEOUT = 300  # 5 menit tanpa lagu -> tutup OS otomatis
CACHE_DIR = "/tmp/kaelen_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

assistant: Client | None = None
call_py: PyTgCalls | None = None
_tg_bot_lock: asyncio.Lock | None = None
_bg_tasks: set[asyncio.Task] = set()
_assistant_user_id: int = 0


@dataclass
class TrackItem:
    title: str
    stream_url: str
    webpage_url: str
    duration: str
    requester_id: int
    requester_name: str
    thread_id: int | None = None
    source_label: str = "Music Vault"


@dataclass
class GroupState:
    chat_id: int
    queue: deque[TrackItem] = field(default_factory=deque)
    current_track: TrackItem | None = None
    is_paused: bool = False
    idle_since: float | None = None
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    privileged_cache: set[int] = field(default_factory=lambda: {OWNER_ID})
    privileged_cache_ts: float = 0.0
    call_id: int | None = None
    now_playing_msg_id: int | None = None


_groups: dict[int, GroupState] = {}
_call_id_to_chat: dict[int, int] = {}


def _is_group_allowed(chat_id: int) -> bool:
    return chat_id in ALLOWED_GROUPS


async def _reject_unauthorized_group(chat_id: int):
    logger.warning("Akses ditolak untuk grup tidak terdaftar: %s", chat_id)
    try:
        await bot.send_message(
            chat_id,
            "⛔ Grup ini tidak terdaftar di whitelist bot. Bot akan keluar otomatis.",
        )
    except Exception:
        pass
    try:
        await bot.leave_chat(chat_id)
    except Exception:
        pass


def _get_group_state(chat_id: int) -> GroupState:
    st = _groups.get(chat_id)
    if st is None:
        st = GroupState(chat_id=chat_id)
        _groups[chat_id] = st
    return st


def _spawn_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


def _format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "Live / ∞"
    sec = int(seconds)
    mins, rem = divmod(sec, 60)
    hrs, mins = divmod(mins, 60)
    return f"{hrs}:{mins:02d}:{rem:02d}" if hrs else f"{mins}:{rem:02d}"


def _build_player_keyboard(is_paused: bool) -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton(text="▶️ Resume", callback_data="dj:resume")
        if is_paused
        else InlineKeyboardButton(text="⏸ Pause", callback_data="dj:pause")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                toggle_btn,
                InlineKeyboardButton(text="⏭ Skip", callback_data="dj:skip"),
                InlineKeyboardButton(text="⏹ Stop", callback_data="dj:stop"),
            ],
            [
                InlineKeyboardButton(text="📜 Lihat Antrean", callback_data="dj:queue"),
            ],
        ]
    )


def _format_now_playing_text(st: GroupState) -> str:
    track = st.current_track
    if not track:
        return "🔇 Tidak ada lagu yang sedang diputar."
    state_label = "⏸ Dijeda" if st.is_paused else "▶️ Sedang Diputar"
    link_part = (
        f'<a href="{html.escape(track.webpage_url)}">{html.escape(track.title)}</a>'
        if track.webpage_url
        else f"<b>{html.escape(track.title)}</b>"
    )
    return (
        f"🎧 <b>Now Playing — {state_label}</b> (📀 {html.escape(track.source_label)})\n"
        f"• Lagu: {link_part} (<code>{track.duration}</code>)\n"
        f"• Permintaan: {_mention(track.requester_id, track.requester_name)}\n"
        f"• Antrean berikutnya: <b>{len(st.queue)}</b> lagu"
    )


def _is_actively_playing_in_os(chat_id: int) -> bool:
    st = _get_group_state(chat_id)
    if not call_py or st.current_track is None:
        return False
    try:
        active_calls = getattr(call_py, "calls", None)
        if isinstance(active_calls, dict):
            return chat_id in active_calls
    except Exception:
        pass
    return True


def _cleanup_old_cache(keep_path: str | None = None):
    try:
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


async def react_msg(chat_id: int, message_id: int, emoji: str = "🎧"):
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:
        pass


def _schedule_delete(chat_id: int, message_ids: list[int], delay: int = AUTO_DELETE_DELAY):
    async def _worker():
        await asyncio.sleep(delay)
        for mid in message_ids:
            if not mid:
                continue
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

    _spawn_task(_worker())


async def _send_group_msg(
    chat_id: int,
    text: str,
    thread_id: int | None = None,
    auto_delete: int = AUTO_DELETE_DELAY,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    clean_tid = thread_id if (thread_id and thread_id != 1) else None
    attempts = [clean_tid, None] if clean_tid else [None]
    for tid in attempts:
        kwargs = {"message_thread_id": tid} if tid else {}
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
                **kwargs,
            )
            if sent and auto_delete > 0:
                _schedule_delete(chat_id, [sent.message_id], delay=auto_delete)
            return sent
        except Exception as e:
            if tid and "thread not found" in str(e).lower():
                continue
            logger.warning("Gagal kirim pesan ke grup %s: %s", chat_id, e)
            return None


# --- AUTO-JOIN AKUN ASISTEN KE GRUP WHITELIST ---
async def _ensure_assistant_in_group(chat_id: int) -> bool:
    if not _is_group_allowed(chat_id):
        return False

    try:
        await assistant.get_chat_member(chat_id, _assistant_user_id)
        return True
    except Exception:
        pass

    try:
        invite = await bot.create_chat_invite_link(chat_id=chat_id, member_limit=1)
        await assistant.join_chat(invite.invite_link)
        try:
            await bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=invite.invite_link)
        except Exception:
            pass
        await asyncio.sleep(1.0)
        return True
    except UserAlreadyParticipant:
        return True
    except Exception as e:
        logger.warning("Asisten gagal auto-join grup %s: %s", chat_id, e)
        return False


# --- STRICT LISTEN-ONLY OS PER GRUP ---
async def _get_privileged_os_user_ids(chat_id: int, force_refresh: bool = False) -> set[int]:
    st = _get_group_state(chat_id)
    now = time.monotonic()
    if not force_refresh and (now - st.privileged_cache_ts) < 60.0 and st.privileged_cache:
        priv = set(st.privileged_cache)
        priv.add(OWNER_ID)
        if _assistant_user_id:
            priv.add(_assistant_user_id)
        return priv

    priv = {OWNER_ID}
    if _assistant_user_id:
        priv.add(_assistant_user_id)
    try:
        admins = await bot.get_chat_administrators(chat_id)
        for m in admins:
            if getattr(m, "user", None) and m.user.id:
                priv.add(int(m.user.id))
        st.privileged_cache = priv
        st.privileged_cache_ts = now
    except Exception as e:
        logger.debug("Gagal memuat daftar admin grup %s: %s", chat_id, e)
        priv.update(st.privileged_cache)
    return priv


async def _is_user_admin_or_owner(chat_id: int, uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, uid)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _mute_all_non_admins(chat_id: int, call):
    if not call:
        return
    try:
        privileged_ids = await _get_privileged_os_user_ids(chat_id)
        res = await assistant.invoke(
            functions.phone.GetGroupParticipants(
                call=call,
                ids=[],
                sources=[],
                offset="",
                limit=200,
            )
        )
        for p in getattr(res, "participants", []):
            peer_uid = getattr(getattr(p, "peer", None), "user_id", None)
            if not peer_uid or peer_uid in privileged_ids:
                continue
            if (
                not getattr(p, "muted", True)
                or getattr(p, "can_self_unmute", False)
                or getattr(p, "raise_hand_rating", None) is not None
            ):
                target_peer = await assistant.resolve_peer(peer_uid)
                await assistant.invoke(
                    functions.phone.EditGroupCallParticipant(
                        call=call,
                        participant=target_peer,
                        muted=True,
                        raise_hand=False,
                    )
                )
    except Exception as e:
        logger.debug("Mute sweep skip (%s): %s", chat_id, e)


async def _close_voice_chat_if_idle(chat_id: int, call) -> bool:
    st = _get_group_state(chat_id)
    if not call:
        st.idle_since = None
        return False

    if st.current_track is not None or st.queue:
        st.idle_since = None
        return False

    now = time.monotonic()
    if st.idle_since is None:
        st.idle_since = now
        return False

    if (now - st.idle_since) < OS_IDLE_TIMEOUT:
        return False

    try:
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        await assistant.invoke(functions.phone.DiscardGroupCall(call=call))
        st.idle_since = None
        if st.call_id:
            _call_id_to_chat.pop(st.call_id, None)
            st.call_id = None
        logger.info("OS di grup %s ditutup otomatis karena idle.", chat_id)
        await _send_group_msg(
            chat_id,
            "🕯️ <i>Tidak ada lagu yang diputar selama 5 menit — Obrolan Suara ditutup otomatis.</i>",
            auto_delete=30,
        )
        return True
    except Exception as e:
        logger.warning("Gagal menutup OS idle di grup %s: %s", chat_id, e)
        return False


async def _periodic_os_mute_enforcer():
    """Sweep seluruh grup aktif setiap 10 detik untuk mute non-admin & tutup OS idle."""
    await asyncio.sleep(5)
    while True:
        for chat_id, st in list(_groups.items()):
            if not _is_group_allowed(chat_id):
                continue
            try:
                if not assistant:
                    continue
                peer = await assistant.resolve_peer(chat_id)
                full = await assistant.invoke(functions.channels.GetFullChannel(channel=peer))
                call = getattr(full.full_chat, "call", None)
                if call:
                    cid = getattr(call, "id", None)
                    if cid:
                        st.call_id = cid
                        _call_id_to_chat[cid] = chat_id

                    closed = await _close_voice_chat_if_idle(chat_id, call)
                    if not closed:
                        try:
                            await assistant.invoke(
                                functions.phone.ToggleGroupCallSettings(
                                    call=call,
                                    join_muted=True,
                                )
                            )
                        except Exception:
                            pass
                        await _mute_all_non_admins(chat_id, call)
                else:
                    st.idle_since = None
                    if st.call_id:
                        _call_id_to_chat.pop(st.call_id, None)
                        st.call_id = None
            except Exception as e:
                logger.debug("Periodic OS sweep error (%s): %s", chat_id, e)

        await asyncio.sleep(OS_MUTE_SWEEP_INTERVAL)


async def _ensure_voice_chat_open_and_muted(chat_id: int):
    if not _is_group_allowed(chat_id):
        return None
    st = _get_group_state(chat_id)
    try:
        joined = await _ensure_assistant_in_group(chat_id)
        if not joined:
            return None

        peer = await assistant.resolve_peer(chat_id)
        full = await assistant.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full.full_chat, "call", None)

        if not call:
            logger.info("OS belum terbuka di grup %s. Membuka Obrolan Suara...", chat_id)
            await assistant.invoke(
                functions.phone.CreateGroupCall(
                    peer=peer,
                    random_id=assistant.rnd_id(),
                    title="🎶 Music Lounge — DJ Booth",
                )
            )
            await asyncio.sleep(1.0)
            full = await assistant.invoke(functions.channels.GetFullChannel(channel=peer))
            call = getattr(full.full_chat, "call", None)

        if call:
            cid = getattr(call, "id", None)
            if cid:
                st.call_id = cid
                _call_id_to_chat[cid] = chat_id
            try:
                await assistant.invoke(
                    functions.phone.ToggleGroupCallSettings(
                        call=call,
                        join_muted=True,
                    )
                )
            except Exception:
                pass
            await _mute_all_non_admins(chat_id, call)
        return call
    except Exception as e:
        logger.error("Gagal membuka/mengatur OS di grup %s: %s", chat_id, e)
        return None


async def _unmute_assistant_in_call(call):
    if not call or not _assistant_user_id:
        return
    try:
        self_peer = await assistant.resolve_peer(_assistant_user_id)
        await assistant.invoke(
            functions.phone.EditGroupCallParticipant(
                call=call,
                participant=self_peer,
                muted=False,
            )
        )
    except Exception:
        pass


async def _enforce_os_listener_mute(client: Client, update, users, chats):
    if not isinstance(update, raw_types.UpdateGroupCallParticipants):
        return
    try:
        call_id = getattr(update.call, "id", None)
        chat_id = _call_id_to_chat.get(call_id)
        if not chat_id or not _is_group_allowed(chat_id):
            return

        privileged_ids = await _get_privileged_os_user_ids(chat_id)
        for p in update.participants:
            peer_uid = getattr(getattr(p, "peer", None), "user_id", None)
            if not peer_uid or peer_uid in privileged_ids:
                continue

            just_joined = getattr(p, "just_joined", False)
            is_unmuted = not getattr(p, "muted", True)
            can_unmute = getattr(p, "can_self_unmute", False)
            raised_hand = getattr(p, "raise_hand_rating", None) is not None

            if just_joined or is_unmuted or can_unmute or raised_hand:
                target_peer = await client.resolve_peer(peer_uid)
                await client.invoke(
                    functions.phone.EditGroupCallParticipant(
                        call=update.call,
                        participant=target_peer,
                        muted=True,
                        raise_hand=False,
                    )
                )
    except Exception:
        pass


# --- JALUR 1: MUSIC_VAULT_ID ---
async def _search_music_vault(query: str) -> dict | None:
    if not MUSIC_VAULT_ID:
        return None
    try:
        async for msg in assistant.search_messages(chat_id=MUSIC_VAULT_ID, query=query, limit=10):
            is_audio, display_title, duration_sec = _extract_audio_meta(msg, query)
            if not is_audio:
                continue

            dest_path = os.path.join(CACHE_DIR, f"vault_{msg.id}_{int(datetime.utcnow().timestamp())}")
            downloaded_path = await assistant.download_media(msg, file_name=dest_path)
            if downloaded_path and os.path.exists(downloaded_path):
                _cleanup_old_cache(keep_path=downloaded_path)
                return {
                    "title": display_title,
                    "stream_url": downloaded_path,
                    "webpage_url": msg.link or "",
                    "duration": _format_duration(duration_sec),
                    "source_label": "Music Vault",
                }
    except Exception as e:
        logger.warning("Gagal mencari di MUSIC_VAULT_ID (%s): %s", MUSIC_VAULT_ID, e)
    return None


# --- JALUR 2: BOT MUSIK TELEGRAM -> SIMPAN KE MUSIC_VAULT_ID ---
async def _fetch_via_tg_music_bot(query: str) -> dict | None:
    music_bots = ["vkmusic_bot", "SpotSeekBot"]
    async with _tg_bot_lock:
        for bot_usn in music_bots:
            try:
                last_id = 0
                async for m in assistant.get_chat_history(bot_usn, limit=1):
                    last_id = m.id

                await assistant.send_message(bot_usn, query)
                target_audio_msg = None
                clicked = False

                for _ in range(12):
                    await asyncio.sleep(1.0)
                    recent = []
                    async for m in assistant.get_chat_history(bot_usn, limit=6):
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
                                            await assistant.request_callback_answer(
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
                        copied = await assistant.copy_message(
                            chat_id=MUSIC_VAULT_ID,
                            from_chat_id=bot_usn,
                            message_id=target_audio_msg.id,
                        )
                        archived_link = getattr(copied, "link", "") or ""
                    except Exception:
                        pass

                dest_path = os.path.join(CACHE_DIR, f"tgbot_{target_audio_msg.id}_{int(datetime.utcnow().timestamp())}")
                downloaded_path = await assistant.download_media(target_audio_msg, file_name=dest_path)
                if downloaded_path and os.path.exists(downloaded_path):
                    _cleanup_old_cache(keep_path=downloaded_path)
                    return {
                        "title": display_title,
                        "stream_url": downloaded_path,
                        "webpage_url": archived_link,
                        "duration": _format_duration(duration_sec),
                        "source_label": "Music Vault",
                    }
            except Exception as e:
                logger.warning("Gagal ambil dari @%s: %s", bot_usn, e)
                continue
    return None


# --- JALUR 3: STREAMING CADANGAN ---
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
                "duration": _format_duration(song.get("duration")),
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


async def _play_next_in_queue(chat_id: int):
    st = _get_group_state(chat_id)
    async with st.play_lock:
        if st.now_playing_msg_id:
            try:
                await bot.delete_message(chat_id, st.now_playing_msg_id)
            except Exception:
                pass
            st.now_playing_msg_id = None

        if not st.queue:
            st.current_track = None
            st.is_paused = False
            st.idle_since = time.monotonic()
            try:
                await call_py.leave_call(chat_id)
            except Exception:
                pass
            return

        next_track = st.queue.popleft()
        st.current_track = next_track
        st.is_paused = False
        st.idle_since = None

        try:
            call = await _ensure_voice_chat_open_and_muted(chat_id)
            if not call:
                raise RuntimeError("Pastikan Akun Asisten sudah jadi Admin dengan izin Manage Voice Chats.")

            stream = MediaStream(
                next_track.stream_url,
                audio_parameters=AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
            )
            await call_py.play(chat_id, stream)
            await _unmute_assistant_in_call(call)
            await _mute_all_non_admins(chat_id, call)

            sent = await _send_group_msg(
                chat_id,
                _format_now_playing_text(st),
                thread_id=next_track.thread_id,
                auto_delete=0,
                reply_markup=_build_player_keyboard(is_paused=False),
            )
            if sent:
                st.now_playing_msg_id = sent.message_id
        except Exception as e:
            logger.error("PyTgCalls play error di grup %s: %s", chat_id, e)
            await _send_group_msg(
                chat_id,
                f"⚠️ Gagal memutar <b>{html.escape(next_track.title)}</b> di OS.\n"
                f"<i>({html.escape(str(e))})</i>",
                thread_id=next_track.thread_id,
                auto_delete=30,
            )
            st.current_track = None
            st.idle_since = time.monotonic()
            if st.queue:
                _spawn_task(_play_next_in_queue(chat_id))


async def on_tgcalls_update(_, update):
    update_name = type(update).__name__
    if "StreamEnded" in update_name or "StreamAudioEnded" in update_name:
        chat_id = getattr(update, "chat_id", None)
        if chat_id and _is_group_allowed(int(chat_id)):
            await _play_next_in_queue(int(chat_id))


async def enqueue_song_request(
    chat_id: int,
    query: str,
    uid: int,
    requester_name: str,
    thread_id: int | None = None,
    announce_queue: bool = True,
) -> bool:
    st = _get_group_state(chat_id)

    info = await resolve_audio_track(query)
    if not info:
        await _send_group_msg(
            chat_id,
            f"🥀 Lagu tidak ditemukan untuk <b>{html.escape(query)}</b>.",
            thread_id=thread_id,
            auto_delete=25,
        )
        return False

    track = TrackItem(
        title=info["title"],
        stream_url=info["stream_url"],
        webpage_url=info["webpage_url"],
        duration=info["duration"],
        requester_id=uid,
        requester_name=requester_name,
        thread_id=thread_id,
        source_label=info.get("source_label", "Music Vault"),
    )

    if st.current_track is not None and not _is_actively_playing_in_os(chat_id):
        st.current_track = None
        st.is_paused = False

    st.idle_since = None
    st.queue.append(track)

    if st.current_track is None:
        await _play_next_in_queue(chat_id)
    elif announce_queue:
        pos = len(st.queue)
        await _send_group_msg(
            chat_id,
            f"💿 <b>Ditambahkan ke Antrean (#{pos})</b>\n"
            f"• Lagu: <b>{html.escape(track.title)}</b> (<code>{track.duration}</code>)\n"
            f"• Oleh: {_mention(uid, requester_name)}",
            thread_id=thread_id,
            auto_delete=35,
        )
    return True


# --- AUTO-LEAVE SAAT BOT DITAMBAHKAN KE GRUP NON-WHITELIST ---
@dp.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def on_bot_added_to_group(event: ChatMemberUpdated):
    chat_id = event.chat.id
    new_status = event.new_chat_member.status
    if new_status in ("member", "administrator") and not _is_group_allowed(chat_id):
        await _reject_unauthorized_group(chat_id)


# --- CALLBACK HANDLER UNTUK TOMBOL INLINE PLAYER ---
@dp.callback_query(F.data.startswith("dj:"))
async def on_player_button_click(callback: CallbackQuery):
    if not callback.message or callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    if not _is_group_allowed(chat_id):
        await callback.answer("⛔ Grup tidak terdaftar.", show_alert=True)
        return

    st = _get_group_state(chat_id)
    uid = callback.from_user.id
    action = callback.data.split(":", 1)[1]

    if action == "queue":
        if not st.current_track and not st.queue:
            await callback.answer("📭 Daftar antrean kosong.", show_alert=True)
            return
        lines = []
        if st.current_track:
            lines.append(f"▶️ {st.current_track.title[:38]}")
        for idx, t in enumerate(list(st.queue)[:6], start=1):
            lines.append(f"{idx}. {t.title[:35]}")
        if len(st.queue) > 6:
            lines.append(f"... +{len(st.queue) - 6} lagu lainnya")
        await callback.answer("\n".join(lines)[:200], show_alert=True)
        return

    if not st.current_track:
        await callback.answer("🔇 Tidak ada lagu yang sedang diputar.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    is_admin = await _is_user_admin_or_owner(chat_id, uid)
    is_requester = uid == st.current_track.requester_id

    if action == "stop":
        if not is_admin:
            await callback.answer("⛔ Hanya Admin atau Owner yang bisa menghentikan musik.", show_alert=True)
            return
        st.queue.clear()
        st.current_track = None
        st.is_paused = False
        st.idle_since = time.monotonic()
        st.now_playing_msg_id = None
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        await callback.answer("⏹️ Musik dihentikan.")
        try:
            await callback.message.edit_text(
                f"⏹️ Musik dihentikan oleh {_mention(uid, callback.from_user.first_name)}.",
                parse_mode=ParseMode.HTML,
            )
            _schedule_delete(chat_id, [callback.message.message_id], delay=15)
        except Exception:
            pass
        return

    if not (is_admin or is_requester):
        await callback.answer("⛔ Hanya pemesan lagu atau Admin yang bisa memakai tombol ini.", show_alert=True)
        return

    if action == "pause":
        if st.is_paused:
            await callback.answer("Musik sudah dijeda.")
            return
        try:
            await call_py.pause(chat_id)
            st.is_paused = True
            await callback.answer("⏸️ Musik dijeda.")
            await callback.message.edit_text(
                _format_now_playing_text(st),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_build_player_keyboard(is_paused=True),
            )
        except Exception as e:
            await callback.answer(f"Gagal menjeda: {e}", show_alert=True)

    elif action == "resume":
        if not st.is_paused:
            await callback.answer("Musik sedang diputar.")
            return
        try:
            await call_py.resume(chat_id)
            st.is_paused = False
            await callback.answer("▶️ Musik dilanjutkan.")
            await callback.message.edit_text(
                _format_now_playing_text(st),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_build_player_keyboard(is_paused=False),
            )
        except Exception as e:
            await callback.answer(f"Gagal melanjutkan: {e}", show_alert=True)

    elif action == "skip":
        skipped_title = st.current_track.title
        await callback.answer(f"⏭️ Melewati: {skipped_title[:40]}")
        await _send_group_msg(
            chat_id,
            f"⏭️ <b>{html.escape(skipped_title)}</b> dilewati oleh {_mention(uid, callback.from_user.first_name)}.",
            thread_id=callback.message.message_thread_id,
            auto_delete=20,
        )
        await _play_next_in_queue(chat_id)


# --- COMMAND HANDLERS ---
@dp.message(Command("play", "reqsong"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_play_song(message: types.Message):
    chat_id = message.chat.id
    if not _is_group_allowed(chat_id):
        await _reject_unauthorized_group(chat_id)
        return

    user = message.from_user
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        err = await message.reply(
            "🎶 Format: <code>/play Judul Lagu - Penyanyi</code>",
            parse_mode=ParseMode.HTML,
        )
        _schedule_delete(chat_id, [message.message_id, err.message_id], delay=20)
        return

    query = parts[1].strip()[:120]

    await react_msg(chat_id, message.message_id, "🎶")
    search_msg = await message.reply(
        f"🔎 <i>Mencari lagu <b>{html.escape(query)}</b>...</i>",
        parse_mode=ParseMode.HTML,
    )

    ok = await enqueue_song_request(
        chat_id=chat_id,
        query=query,
        uid=user.id,
        requester_name=user.first_name,
        thread_id=message.message_thread_id,
    )
    _schedule_delete(chat_id, [message.message_id, search_msg.message_id], delay=5 if ok else 15)


@dp.message(Command("np", "nowplaying"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_now_playing(message: types.Message):
    chat_id = message.chat.id
    if not _is_group_allowed(chat_id):
        await _reject_unauthorized_group(chat_id)
        return

    st = _get_group_state(chat_id)
    if not st.current_track:
        err = await message.reply(
            "🔇 Tidak ada lagu yang diputar. Ketik <code>/play judul lagu</code> untuk mulai.",
            parse_mode=ParseMode.HTML,
        )
        _schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    if st.now_playing_msg_id:
        try:
            await bot.delete_message(chat_id, st.now_playing_msg_id)
        except Exception:
            pass

    msg = await message.reply(
        _format_now_playing_text(st),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_build_player_keyboard(is_paused=st.is_paused),
    )
    st.now_playing_msg_id = msg.message_id
    _schedule_delete(chat_id, [message.message_id], delay=5)


@dp.message(Command("queue"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_music_queue(message: types.Message):
    chat_id = message.chat.id
    if not _is_group_allowed(chat_id):
        await _reject_unauthorized_group(chat_id)
        return

    st = _get_group_state(chat_id)
    if not st.current_track and not st.queue:
        err = await message.reply("📭 Daftar antrean lagu kosong.")
        _schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    lines = []
    if st.current_track:
        lines.append(f"▶️ <b>Sekarang:</b> {html.escape(st.current_track.title)} ({html.escape(st.current_track.requester_name)})")
    for idx, t in enumerate(list(st.queue)[:10], start=1):
        lines.append(f"{idx}. {html.escape(t.title)} — <i>{html.escape(t.requester_name)}</i>")

    msg = await message.reply("🎶 <b>Daftar Antrean Lagu</b>\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML)
    _schedule_delete(chat_id, [message.message_id, msg.message_id], delay=40)


@dp.message(Command("skip"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_skip_song(message: types.Message):
    chat_id = message.chat.id
    if not _is_group_allowed(chat_id):
        await _reject_unauthorized_group(chat_id)
        return

    st = _get_group_state(chat_id)
    if not st.current_track:
        err = await message.reply("Tidak ada lagu yang sedang diputar.")
        _schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    uid = message.from_user.id
    if uid != st.current_track.requester_id and not await _is_user_admin_or_owner(chat_id, uid):
        err = await message.reply("Hanya pemesan lagu ini atau Admin yang bisa melewati lagu.")
        _schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    skipped_title = st.current_track.title
    await react_msg(chat_id, message.message_id, "⏭️")
    msg = await message.reply(f"⏭️ Melewati lagu <b>{html.escape(skipped_title)}</b>.", parse_mode=ParseMode.HTML)
    _schedule_delete(chat_id, [message.message_id, msg.message_id], delay=20)
    await _play_next_in_queue(chat_id)


@dp.message(Command("pause"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_pause_song(message: types.Message):
    chat_id = message.chat.id
    if not _is_group_allowed(chat_id):
        await _reject_unauthorized_group(chat_id)
        return

    st = _get_group_state(chat_id)
    if not st.current_track or st.is_paused:
        return
    try:
        await call_py.pause(chat_id)
        st.is_paused = True
        if st.now_playing_msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=st.now_playing_msg_id,
                    text=_format_now_playing_text(st),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=_build_player_keyboard(is_paused=True),
                )
            except Exception:
                pass
        msg = await message.reply("⏸️ Musik dijeda.")
        _schedule_delete(chat_id, [message.message_id, msg.message_id], delay=15)
    except Exception as e:
        err = await message.reply(f"Gagal menjeda: {e}")
        _schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)


@dp.message(Command("resume"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_resume_song(message: types.Message):
    chat_id = message.chat.id
    if not _is_group_allowed(chat_id):
        await _reject_unauthorized_group(chat_id)
        return

    st = _get_group_state(chat_id)
    if not st.current_track or not st.is_paused:
        return
    try:
        await call_py.resume(chat_id)
        st.is_paused = False
        if st.now_playing_msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=st.now_playing_msg_id,
                    text=_format_now_playing_text(st),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=_build_player_keyboard(is_paused=False),
                )
            except Exception:
                pass
        msg = await message.reply("▶️ Musik dilanjutkan.")
        _schedule_delete(chat_id, [message.message_id, msg.message_id], delay=15)
    except Exception as e:
        err = await message.reply(f"Gagal melanjutkan: {e}")
        _schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)


@dp.message(Command("stop"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_stop_music(message: types.Message):
    chat_id = message.chat.id
    if not _is_group_allowed(chat_id):
        await _reject_unauthorized_group(chat_id)
        return

    st = _get_group_state(chat_id)
    uid = message.from_user.id
    if not await _is_user_admin_or_owner(chat_id, uid):
        err = await message.reply("Perintah <code>/stop</code> hanya untuk Admin atau Owner.", parse_mode=ParseMode.HTML)
        _schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    if st.now_playing_msg_id:
        try:
            await bot.delete_message(chat_id, st.now_playing_msg_id)
        except Exception:
            pass
        st.now_playing_msg_id = None

    st.queue.clear()
    st.current_track = None
    st.is_paused = False
    st.idle_since = time.monotonic()

    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass

    msg = await message.reply("⏹️ Musik dihentikan dan antrean dikosongkan.")
    _schedule_delete(chat_id, [message.message_id, msg.message_id], delay=25)


async def register_commands():
    cmds = [
        BotCommand(command="play", description="Putar lagu di Obrolan Suara"),
        BotCommand(command="np", description="Lihat & kontrol lagu yang sedang diputar"),
        BotCommand(command="queue", description="Lihat daftar antrean lagu"),
        BotCommand(command="skip", description="Lewati lagu saat ini"),
        BotCommand(command="pause", description="Jeda musik di OS"),
        BotCommand(command="resume", description="Lanjutkan musik yang dijeda"),
        BotCommand(command="stop", description="[Admin] Hentikan musik & bersihkan antrean"),
    ]
    await bot.set_my_commands(cmds, scope=BotCommandScopeAllGroupChats())


async def main():
    global assistant, call_py, _tg_bot_lock, _assistant_user_id

    _tg_bot_lock = asyncio.Lock()
    await register_commands()

    assistant = Client(
        name="music_assistant",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
    )
    assistant.on_raw_update()(_enforce_os_listener_mute)

    call_py = PyTgCalls(assistant)
    call_py.on_update()(on_tgcalls_update)

    await assistant.start()
    me = await assistant.get_me()
    _assistant_user_id = me.id
    await call_py.start()
    logger.info(
        "Akun Asisten Multi-Group (@%s / %s) aktif! ALLOWED_GROUPS: %s | MUSIC_VAULT_ID: %s",
        me.username or me.first_name,
        me.id,
        sorted(ALLOWED_GROUPS) if ALLOWED_GROUPS else "KOSONG (Semua grup diblokir)",
        MUSIC_VAULT_ID,
    )

    _spawn_task(_periodic_os_mute_enforcer())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
