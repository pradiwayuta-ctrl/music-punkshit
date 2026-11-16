import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("kaelen_dj")

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"].strip()
SESSION_STRING = os.environ["SESSION_STRING"].strip()
OWNER_ID = int(os.environ["OWNER_ID"])


def _parse_allowed_groups() -> set[int]:
    raw = os.environ.get("ALLOWED_GROUPS", "")
    allowed: set[int] = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if token.lstrip("-").isdigit():
            val = int(token)
            allowed.add(val)
            if val > 0:
                allowed.add(int(f"-100{val}"))
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
```[cite: 1]

---

### 5. `kaelen_dj/state.py`
```python
import os
import glob
import asyncio
from collections import deque
from dataclasses import dataclass, field
from kaelen_dj.config import OWNER_ID, ALLOWED_GROUPS, CACHE_DIR


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
    enqueue_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    privileged_cache: set[int] = field(default_factory=lambda: {OWNER_ID})
    privileged_cache_ts: float = 0.0
    call_id: int | None = None
    now_playing_msg_id: int | None = None


groups: dict[int, GroupState] = {}
call_id_to_chat: dict[int, int] = {}
bg_tasks: set[asyncio.Task] = set()


def is_group_allowed(chat_id: int) -> bool:
    if not ALLOWED_GROUPS:
        return False
    return chat_id in ALLOWED_GROUPS


def get_group_state(chat_id: int) -> GroupState:
    st = groups.get(chat_id)
    if st is None:
        st = GroupState(chat_id=chat_id)
        groups[chat_id] = st
    return st


def spawn_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return task


def cleanup_old_cache(keep_path: str | None = None):
    try:
        active_paths = {
            os.path.abspath(st.current_track.stream_url)
            for st in groups.values()
            if st.current_track and os.path.exists(st.current_track.stream_url)
        }
        for st in groups.values():
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
```[cite: 1]

---

### 6. `kaelen_dj/os_guard.py`
```python
import time
import asyncio
from aiogram import Bot
from aiogram.enums import ParseMode
from pyrogram import Client
from pyrogram.errors import UserAlreadyParticipant
from pyrogram.raw import functions, types as raw_types
from pytgcalls import PyTgCalls

from kaelen_dj.config import OWNER_ID, OS_MUTE_SWEEP_INTERVAL, OS_IDLE_TIMEOUT, logger
from kaelen_dj.state import groups, call_id_to_chat, is_group_allowed, get_group_state

bot_ref: Bot | None = None
assistant_ref: Client | None = None
call_py_ref: PyTgCalls | None = None
assistant_user_id: int = 0


def init_os_guard(bot: Bot, assistant: Client, call_py: PyTgCalls, ass_uid: int):
    global bot_ref, assistant_ref, call_py_ref, assistant_user_id
    bot_ref = bot
    assistant_ref = assistant
    call_py_ref = call_py
    assistant_user_id = ass_uid


async def ensure_assistant_in_group(chat_id: int) -> bool:
    if not is_group_allowed(chat_id) or not assistant_ref or not bot_ref:
        return False

    try:
        await assistant_ref.get_chat_member(chat_id, assistant_user_id)
        return True
    except Exception:
        pass

    try:
        invite = await bot_ref.create_chat_invite_link(chat_id=chat_id, member_limit=1)
        await assistant_ref.join_chat(invite.invite_link)
        try:
            await bot_ref.revoke_chat_invite_link(chat_id=chat_id, invite_link=invite.invite_link)
        except Exception:
            pass
        await asyncio.sleep(1.0)
        return True
    except UserAlreadyParticipant:
        return True
    except Exception as e:
        logger.warning("Asisten gagal auto-join grup %s: %s", chat_id, e)
        return False


async def get_privileged_os_user_ids(chat_id: int, force_refresh: bool = False) -> set[int]:
    st = get_group_state(chat_id)
    now = time.monotonic()
    if not force_refresh and (now - st.privileged_cache_ts) < 60.0 and st.privileged_cache:
        priv = set(st.privileged_cache)
        priv.add(OWNER_ID)
        if assistant_user_id:
            priv.add(assistant_user_id)
        return priv

    priv = {OWNER_ID}
    if assistant_user_id:
        priv.add(assistant_user_id)
    try:
        admins = await bot_ref.get_chat_administrators(chat_id)
        for m in admins:
            if getattr(m, "user", None) and m.user.id:
                priv.add(int(m.user.id))
        st.privileged_cache = priv
        st.privileged_cache_ts = now
    except Exception as e:
        logger.debug("Gagal memuat daftar admin grup %s: %s", chat_id, e)
        priv.update(st.privileged_cache)
    return priv


async def is_user_admin_or_owner(chat_id: int, uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    try:
        member = await bot_ref.get_chat_member(chat_id, uid)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def mute_all_non_admins(chat_id: int, call):
    if not call or not assistant_ref:
        return
    try:
        privileged_ids = await get_privileged_os_user_ids(chat_id)
        res = await assistant_ref.invoke(
            functions.phone.GetGroupParticipants(
                call=call, ids=[], sources=[], offset="", limit=200
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
                target_peer = await assistant_ref.resolve_peer(peer_uid)
                await assistant_ref.invoke(
                    functions.phone.EditGroupCallParticipant(
                        call=call,
                        participant=target_peer,
                        muted=True,
                        raise_hand=False,
                    )
                )
    except Exception as e:
        logger.debug("Mute sweep skip (%s): %s", chat_id, e)


async def close_voice_chat_if_idle(chat_id: int, call) -> bool:
    st = get_group_state(chat_id)
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
            await call_py_ref.leave_call(chat_id)
        except Exception:
            pass
        await assistant_ref.invoke(functions.phone.DiscardGroupCall(call=call))
        st.idle_since = None
        if st.call_id:
            call_id_to_chat.pop(st.call_id, None)
            st.call_id = None
        logger.info("OS di grup %s ditutup otomatis karena idle.", chat_id)
        await bot_ref.send_message(
            chat_id,
            "🕯️ <i>Lima menit berlalu tanpa alunan nada. Aku tutup dulu Obrolan Suara malam ini—panggil aku kapan saja kau ingin mendengar musik lagi.</i>",
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as e:
        logger.warning("Gagal menutup OS idle di grup %s: %s", chat_id, e)
        return False


async def periodic_os_mute_enforcer():
    await asyncio.sleep(5)
    while True:
        for chat_id, st in list(groups.items()):
            if not is_group_allowed(chat_id) or not assistant_ref:
                continue
            try:
                peer = await assistant_ref.resolve_peer(chat_id)
                full = await assistant_ref.invoke(functions.channels.GetFullChannel(channel=peer))
                call = getattr(full.full_chat, "call", None)
                if call:
                    cid = getattr(call, "id", None)
                    if cid:
                        st.call_id = cid
                        call_id_to_chat[cid] = chat_id

                    closed = await close_voice_chat_if_idle(chat_id, call)
                    if not closed:
                        try:
                            await assistant_ref.invoke(
                                functions.phone.ToggleGroupCallSettings(call=call, join_muted=True)
                            )
                        except Exception:
                            pass
                        await mute_all_non_admins(chat_id, call)
                else:
                    st.idle_since = None
                    if st.call_id:
                        call_id_to_chat.pop(st.call_id, None)
                        st.call_id = None
            except Exception as e:
                logger.debug("Periodic OS sweep error (%s): %s", chat_id, e)

        await asyncio.sleep(OS_MUTE_SWEEP_INTERVAL)


async def ensure_voice_chat_open_and_muted(chat_id: int):
    if not is_group_allowed(chat_id) or not assistant_ref:
        return None
    st = get_group_state(chat_id)
    try:
        joined = await ensure_assistant_in_group(chat_id)
        if not joined:
            return None

        peer = await assistant_ref.resolve_peer(chat_id)
        full = await assistant_ref.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full.full_chat, "call", None)

        if not call:
            logger.info("OS belum terbuka di grup %s. Membuka Obrolan Suara...", chat_id)
            await assistant_ref.invoke(
                functions.phone.CreateGroupCall(
                    peer=peer,
                    random_id=assistant_ref.rnd_id(),
                    title="🍷 The Velvet Lounge — Kaelen's Booth",
                )
            )
            await asyncio.sleep(1.0)
            full = await assistant_ref.invoke(functions.channels.GetFullChannel(channel=peer))
            call = getattr(full.full_chat, "call", None)

        if call:
            cid = getattr(call, "id", None)
            if cid:
                st.call_id = cid
                call_id_to_chat[cid] = chat_id
            try:
                await assistant_ref.invoke(
                    functions.phone.ToggleGroupCallSettings(call=call, join_muted=True)
                )
            except Exception:
                pass
            await mute_all_non_admins(chat_id, call)
        return call
    except Exception as e:
        logger.error("Gagal membuka/mengatur OS di grup %s: %s", chat_id, e)
        return None


async def unmute_assistant_in_call(call):
    if not call or not assistant_user_id or not assistant_ref:
        return
    try:
        self_peer = await assistant_ref.resolve_peer(assistant_user_id)
        await assistant_ref.invoke(
            functions.phone.EditGroupCallParticipant(
                call=call, participant=self_peer, muted=False
            )
        )
    except Exception:
        pass


async def enforce_os_listener_mute(client: Client, update, users, chats):
    if not isinstance(update, raw_types.UpdateGroupCallParticipants):
        return
    try:
        call_id = getattr(update.call, "id", None)
        chat_id = call_id_to_chat.get(call_id)
        if not chat_id or not is_group_allowed(chat_id):
            return

        privileged_ids = await get_privileged_os_user_ids(chat_id)
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
```[cite: 1]

---

### 7. `kaelen_dj/music_finder.py`
```python
import os
import json
import html
import asyncio
import urllib.parse
import urllib.request
from datetime import datetime
from pyrogram import Client

from kaelen_dj.config import MUSIC_VAULT_ID, CACHE_DIR, logger
from kaelen_dj.state import cleanup_old_cache

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
```[cite: 1]

---

### 8. `kaelen_dj/player.py`
```python
import html
import time
import asyncio
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality, StreamEnded

from kaelen_dj.config import AUTO_DELETE_DELAY, logger
from kaelen_dj.state import GroupState, TrackItem, get_group_state, is_group_allowed, spawn_task
from kaelen_dj.os_guard import ensure_voice_chat_open_and_muted, unmute_assistant_in_call, mute_all_non_admins
from kaelen_dj.music_finder import resolve_audio_track

bot_ref: Bot | None = None
call_py_ref: PyTgCalls | None = None


def init_player(bot: Bot, call_py: PyTgCalls):
    global bot_ref, call_py_ref
    bot_ref = bot
    call_py_ref = call_py


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name or "Tamu")}</a>'


async def react_msg(chat_id: int, message_id: int, emoji: str = "🍷"):
    try:
        await bot_ref.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:
        pass


def schedule_delete(chat_id: int, message_ids: list[int], delay: int = AUTO_DELETE_DELAY):
    async def _worker():
        await asyncio.sleep(delay)
        for mid in message_ids:
            if not mid:
                continue
            try:
                await bot_ref.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

    spawn_task(_worker())


async def send_group_msg(
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
            sent = await bot_ref.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
                **kwargs,
            )
            if sent and auto_delete > 0:
                schedule_delete(chat_id, [sent.message_id], delay=auto_delete)
            return sent
        except Exception as e:
            if tid and "thread not found" in str(e).lower():
                continue
            logger.warning("Gagal kirim pesan ke grup %s: %s", chat_id, e)
            return None


def build_player_keyboard(is_paused: bool) -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton(text="▶️ Lanjutkan", callback_data="dj:resume")
        if is_paused
        else InlineKeyboardButton(text="⏸ Jeda", callback_data="dj:pause")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                toggle_btn,
                InlineKeyboardButton(text="⏭ Lewati", callback_data="dj:skip"),
                InlineKeyboardButton(text="⏹ Hentikan", callback_data="dj:stop"),
            ],
            [
                InlineKeyboardButton(text="📜 Daftar Antrean", callback_data="dj:queue"),
            ],
        ]
    )


def format_now_playing_text(st: GroupState) -> str:
    track = st.current_track
    if not track:
        return "🕯️ <i>Ruangan sedang hening. Belum ada piringan hitam yang berputar.</i>"
    state_label = "⏸ Dijeda" if st.is_paused else "🎶 Sedang Mengalun"
    link_part = (
        f'<a href="{html.escape(track.webpage_url)}">{html.escape(track.title)}</a>'
        if track.webpage_url
        else f"<b>{html.escape(track.title)}</b>"
    )
    return (
        f"🎧 <b>Kaelen's Lounge — {state_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Trek:</b> {link_part} (<code>{track.duration}</code>)\n"
        f"• <b>Pilihan:</b> {mention(track.requester_id, track.requester_name)}\n"
        f"• <b>Sumber:</b> {html.escape(track.source_label)} • <b>Antrean:</b> {len(st.queue)} lagu"
    )


def is_actively_playing_in_os(chat_id: int) -> bool:
    st = get_group_state(chat_id)
    if not call_py_ref or st.current_track is None:
        return False
    try:
        active_calls = getattr(call_py_ref, "calls", None)
        if isinstance(active_calls, dict):
            return chat_id in active_calls
    except Exception:
        pass
    return True


async def play_next_in_queue(chat_id: int):
    st = get_group_state(chat_id)
    async with st.play_lock:
        if st.now_playing_msg_id:
            try:
                await bot_ref.delete_message(chat_id, st.now_playing_msg_id)
            except Exception:
                pass
            st.now_playing_msg_id = None

        if not st.queue:
            st.current_track = None
            st.is_paused = False
            st.idle_since = time.monotonic()
            try:
                await call_py_ref.leave_call(chat_id)
            except Exception:
                pass
            return

        next_track = st.queue.popleft()
        st.current_track = next_track
        st.is_paused = False
        st.idle_since = None

        try:
            call = await ensure_voice_chat_open_and_muted(chat_id)
            if not call:
                raise RuntimeError(
                    "Akun Asisten belum memiliki akses Admin dengan izin Manage Voice Chats."
                )

            stream = MediaStream(
                next_track.stream_url,
                audio_parameters=AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
            )
            await call_py_ref.play(chat_id, stream)
            await unmute_assistant_in_call(call)
            await mute_all_non_admins(chat_id, call)

            sent = await send_group_msg(
                chat_id,
                format_now_playing_text(st),
                thread_id=next_track.thread_id,
                auto_delete=0,
                reply_markup=build_player_keyboard(is_paused=False),
            )
            if sent:
                st.now_playing_msg_id = sent.message_id
        except Exception as e:
            logger.error("PyTgCalls play error di grup %s: %s", chat_id, e)
            await send_group_msg(
                chat_id,
                f"⚠️ Maaf, piringan hitam untuk <b>{html.escape(next_track.title)}</b> gagal diputar.\n"
                f"<i>Alasan: {html.escape(str(e))}</i>",
                thread_id=next_track.thread_id,
                auto_delete=30,
            )
            st.current_track = None
            st.idle_since = time.monotonic()
            if st.queue:
                spawn_task(play_next_in_queue(chat_id))


async def on_tgcalls_stream_ended(_, update: StreamEnded):
    chat_id = getattr(update, "chat_id", None)
    if chat_id and is_group_allowed(int(chat_id)):
        await play_next_in_queue(int(chat_id))


async def enqueue_song_request(
    chat_id: int,
    query: str,
    uid: int,
    requester_name: str,
    thread_id: int | None = None,
    announce_queue: bool = True,
) -> bool:
    st = get_group_state(chat_id)

    info = await resolve_audio_track(query)
    if not info:
        await send_group_msg(
            chat_id,
            f"🥀 Aku sudah mencari ke seluruh rak arsip, tapi <b>{html.escape(query)}</b> tidak ditemukan.",
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

    should_start_now = False
    pos = 0
    async with st.enqueue_lock:
        if st.current_track is not None and not is_actively_playing_in_os(chat_id):
            st.current_track = None
            st.is_paused = False

        st.idle_since = None
        st.queue.append(track)
        if st.current_track is None:
            should_start_now = True
        else:
            pos = len(st.queue)

    if should_start_now:
        await play_next_in_queue(chat_id)
    elif announce_queue:
        await send_group_msg(
            chat_id,
            f"💿 <b>Masuk Daftar Putar (#{pos})</b>\n"
            f"• <b>Lagu:</b> {html.escape(track.title)} (<code>{track.duration}</code>)\n"
            f"• <b>Pesanan:</b> {mention(uid, requester_name)}\n"
            f"<i>Duduk santai dulu, gilirannya akan segera tiba.</i>",
            thread_id=thread_id,
            auto_delete=35,
        )
    return True
```[cite: 1]

---

### 9. `kaelen_dj/handlers.py`
```python
import html
import time
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.enums import ParseMode

from kaelen_dj.config import OWNER_ID, logger
from kaelen_dj.state import is_group_allowed, get_group_state
from kaelen_dj.os_guard import ensure_assistant_in_group, is_user_admin_or_owner
from kaelen_dj import player

router = Router()


async def reject_unauthorized_group(bot, chat_id: int):
    logger.warning("Akses ditolak untuk grup tidak terdaftar: %s", chat_id)
    try:
        await bot.send_message(
            chat_id,
            "🍷 <i>Maaf, ruangan ini tidak masuk dalam daftar tamu privatku. Aku permisi undur diri.</i>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    try:
        await bot.leave_chat(chat_id)
    except Exception:
        pass


def _private_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📖 Panduan & Contoh", callback_data="pc:tutor"),
                InlineKeyboardButton(text="💬 Kirim Feedback", callback_data="pc:feedback"),
            ]
        ]
    )


# --- GREETING & TUTORIAL DI PRIVATE CHAT (PC BOT) ---
@router.message(Command("start", "help"), F.chat.type == "private")
async def cmd_start_private(message: types.Message):
    user_mention = player.mention(message.from_user.id, message.from_user.first_name)
    text = (
        f"🍷 <b>Selamat datang di balik tirai, {user_mention}.</b>\n\n"
        f"Aku <b>Kaelen Thorne</b>, penjaga booth musik di <i>The Velvet Lounge</i>. "
        f"Aku bertugas mengatur atmosfer dan memutar musik secara eksklusif di grup yang telah terdaftar.\n\n"
        f"<b>Ringkasan Perintah di Grup:</b>\n"
        f"• <code>/play judul lagu</code> — Memutar / menambah lagu ke antrean\n"
        f"• <code>/np</code> — Menampilkan panel kontrol pemutar aktif\n"
        f"• <code>/queue</code> — Melihat daftar antrean lagu\n"
        f"• <code>/skip</code> — Melewati lagu yang sedang berputar\n"
        f"• <code>/pause</code> & <code>/resume</code> — Jeda atau lanjutkan musik\n"
        f"• <code>/stop</code> — <i>[Admin]</i> Hentikan sesi & bersihkan antrean\n\n"
        f"<i>Tekan tombol di bawah untuk melihat contoh penggunaan atau mengirim masukan ke pengelola.</i>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=_private_menu_keyboard())


@router.callback_query(F.data.startswith("pc:"))
async def on_private_menu_click(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    if action == "tutor":
        tutor_text = (
            "📖 <b>Tutorial & Contoh Penggunaan</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ <b>Persiapan Grup:</b>\n"
            "   • Pastikan bot & akun asisten sudah menjadi <b>Admin</b> dengan izin <i>Manage Voice Chats</i> & <i>Invite Users</i>.\n\n"
            "2️⃣ <b>Memutar Lagu:</b>\n"
            "   • Ketik: <code>/play Frank Sinatra - Fly Me To The Moon</code>\n"
            "   • Atau: <code>/play Nadin Amizah - Bertaut</code>\n"
            "   • Jika Obrolan Suara belum aktif, aku akan membukanya otomatis dan membisukan pendengar non-admin.\n\n"
            "3️⃣ <b>Mengatur Pemutaran:</b>\n"
            "   • Gunakan tombol <b>⏸ Jeda</b>, <b>▶️ Lanjutkan</b>, <b>⏭ Lewati</b>, atau <b>⏹ Hentikan</b> langsung di pesan player."
        )
        await callback.message.edit_text(
            tutor_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="pc:home")]]
            ),
        )
    elif action == "feedback":
        fb_text = (
            "💬 <b>Kirim Feedback / Laporan</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Punya saran lagu, masukan fitur, atau menemukan kendala saat memutar musik?\n\n"
            "Kirim pesan di chat ini dengan format:\n"
            "<code>/feedback pesan atau saranmu di sini</code>\n\n"
            "<i>Contoh:</i> <code>/feedback Tolong tambahkan playlist jazz malam hari!</code>"
        )
        await callback.message.edit_text(
            fb_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Kembali", callback_data="pc:home")]]
            ),
        )
    elif action == "home":
        user_mention = player.mention(callback.from_user.id, callback.from_user.first_name)
        await callback.message.edit_text(
            f"🍷 <b>Selamat datang kembali, {user_mention}.</b>\n"
            f"Silakan pilih menu di bawah untuk melihat panduan atau mengirim masukan.",
            parse_mode=ParseMode.HTML,
            reply_markup=_private_menu_keyboard(),
        )
    await callback.answer()


@router.message(Command("feedback"))
async def cmd_feedback(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        err = await message.reply(
            "💬 Tuliskan masukanmu setelah perintah.\nContoh: <code>/feedback Lagu sering putus-putus di grup</code>",
            parse_mode=ParseMode.HTML,
        )
        if message.chat.type != "private":
            player.schedule_delete(message.chat.id, [message.message_id, err.message_id], delay=20)
        return

    fb_content = html.escape(parts[1].strip()[:1000])
    sender = player.mention(message.from_user.id, message.from_user.first_name)
    origin = "Private Chat" if message.chat.type == "private" else f"Grup <b>{html.escape(message.chat.title or str(message.chat.id))}</b> (<code>{message.chat.id}</code>)"

    try:
        await message.bot.send_message(
            OWNER_ID,
            f"📩 <b>Feedback Baru — Kaelen DJ</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Dari:</b> {sender} (<code>{message.from_user.id}</code>)\n"
            f"• <b>Lokasi:</b> {origin}\n"
            f"• <b>Pesan:</b>\n<blockquote>{fb_content}</blockquote>",
            parse_mode=ParseMode.HTML,
        )
        reply_msg = await message.reply(
            "🍷 <i>Terima kasih. Catatanmu sudah kusampaikan langsung ke meja pemilik lounge.</i>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        reply_msg = await message.reply("⚠️ Maaf, gagal meneruskan feedback saat ini.")

    if message.chat.type != "private":
        player.schedule_delete(message.chat.id, [message.message_id, reply_msg.message_id], delay=20)


# --- GREETING & TUTORIAL SAAT BOT MASUK GRUP ---
@router.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def on_bot_added_to_group(event: ChatMemberUpdated):
    chat_id = event.chat.id
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status

    if old_status in ("left", "kicked") and new_status in ("member", "administrator"):
        if not is_group_allowed(chat_id):
            await reject_unauthorized_group(event.bot, chat_id)
            return

        get_group_state(chat_id)
        joined = await ensure_assistant_in_group(chat_id)
        status_note = (
            "✅ Akun asisten sudah bersiap di sudut ruangan."
            if joined
            else "⚠️ <i>Pastikan aku memiliki izin <b>Invite Users</b> & <b>Manage Voice Chats</b> agar asistenku bisa masuk ke Obrolan Suara.</i>"
        )
        await player.send_group_msg(
            chat_id,
            f"🍷 <b>Tirai lounge telah dibuka di {html.escape(event.chat.title or 'ruangan ini')}.</b>\n"
            f"{status_note}\n\n"
            f"📖 <b>Contoh Penggunaan Cepat:</b>\n"
            f"• <code>/play Frank Sinatra - Fly Me To The Moon</code>\n"
            f"• <code>/np</code> — Kontrol pemutar lagu\n"
            f"• <code>/queue</code> — Lihat antrean\n"
            f"• <code>/feedback pesan</code> — Kirim masukan ke pengelola",
            auto_delete=60,
        )


@router.message(Command("start", "help"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_start_group(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await reject_unauthorized_group(message.bot, chat_id)
        return

    user_mention = player.mention(message.from_user.id, message.from_user.first_name)
    text = (
        f"🍷 <b>Malam yang tenang, {user_mention}.</b>\n"
        f"Booth musik sudah siap. Gunakan perintah berikut:\n\n"
        f"• <code>/play judul lagu - penyanyi</code> — Putar / tambah antrean\n"
        f"• <code>/np</code> — Panel kontrol lagu aktif\n"
        f"• <code>/queue</code> — Daftar antrean\n"
        f"• <code>/skip</code>, <code>/pause</code>, <code>/resume</code> — Kendali pemutaran\n"
        f"• <code>/stop</code> — <i>[Admin]</i> Hentikan sesi musik\n"
        f"• <code>/feedback pesan</code> — Kirim saran/laporan"
    )
    sent = await message.reply(text, parse_mode=ParseMode.HTML)
    player.schedule_delete(chat_id, [message.message_id, sent.message_id], delay=40)


# --- CALLBACK HANDLER UNTUK TOMBOL INLINE PLAYER ---
@router.callback_query(F.data.startswith("dj:"))
async def on_player_button_click(callback: CallbackQuery):
    if not callback.message or callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    if not is_group_allowed(chat_id):
        await callback.answer("⛔ Ruangan ini tidak terdaftar.", show_alert=True)
        return

    st = get_group_state(chat_id)
    uid = callback.from_user.id
    action = callback.data.split(":", 1)[1]

    if action == "queue":
        if not st.current_track and not st.queue:
            await callback.answer("📭 Daftar putar masih kosong.", show_alert=True)
            return
        lines = []
        if st.current_track:
            lines.append(f"🎶 {st.current_track.title[:38]}")
        for idx, t in enumerate(list(st.queue)[:6], start=1):
            lines.append(f"{idx}. {t.title[:35]}")
        if len(st.queue) > 6:
            lines.append(f"... +{len(st.queue) - 6} lagu lainnya")
        await callback.answer("\n".join(lines)[:200], show_alert=True)
        return

    if not st.current_track:
        await callback.answer("🕯️ Tidak ada lagu yang sedang berputar.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    is_admin = await is_user_admin_or_owner(chat_id, uid)
    is_requester = uid == st.current_track.requester_id

    if action == "stop":
        if not is_admin:
            await callback.answer("⛔ Hanya pengelola ruangan (Admin) yang bisa menutup sesi ini.", show_alert=True)
            return
        st.queue.clear()
        st.current_track = None
        st.is_paused = False
        st.idle_since = time.monotonic()
        st.now_playing_msg_id = None
        try:
            await player.call_py_ref.leave_call(chat_id)
        except Exception:
            pass
        await callback.answer("⏹️ Sesi musik dihentikan.")
        try:
            await callback.message.edit_text(
                f"⏹️ Piringan hitam dihentikan oleh {player.mention(uid, callback.from_user.first_name)}. Suasana kembali hening.",
                parse_mode=ParseMode.HTML,
            )
            player.schedule_delete(chat_id, [callback.message.message_id], delay=15)
        except Exception:
            pass
        return

    if not (is_admin or is_requester):
        await callback.answer("⛔ Hanya pemesan lagu atau Admin yang memegang kendali trek ini.", show_alert=True)
        return

    if action == "pause":
        if st.is_paused:
            await callback.answer("Alunan musik memang sedang dijeda.")
            return
        try:
            await player.call_py_ref.pause(chat_id)
            st.is_paused = True
            await callback.answer("⏸️ Musik dijeda sejenak.")
            await callback.message.edit_text(
                player.format_now_playing_text(st),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=player.build_player_keyboard(is_paused=True),
            )
        except Exception as e:
            await callback.answer(f"Gagal menjeda: {e}", show_alert=True)

    elif action == "resume":
        if not st.is_paused:
            await callback.answer("Musik sedang mengalun.")
            return
        try:
            await player.call_py_ref.resume(chat_id)
            st.is_paused = False
            await callback.answer("▶️ Melanjutkan alunan musik.")
            await callback.message.edit_text(
                player.format_now_playing_text(st),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=player.build_player_keyboard(is_paused=False),
            )
        except Exception as e:
            await callback.answer(f"Gagal melanjutkan: {e}", show_alert=True)

    elif action == "skip":
        skipped_title = st.current_track.title
        await callback.answer(f"⏭️ Melewati: {skipped_title[:40]}")
        await player.send_group_msg(
            chat_id,
            f"⏭️ <b>{html.escape(skipped_title)}</b> dilewati atas permintaan {player.mention(uid, callback.from_user.first_name)}.",
            thread_id=callback.message.message_thread_id,
            auto_delete=20,
        )
        await player.play_next_in_queue(chat_id)


# --- COMMAND HANDLERS MUSIK ---
@router.message(Command("play", "reqsong"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_play_song(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await reject_unauthorized_group(message.bot, chat_id)
        return

    user = message.from_user
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        err = await message.reply(
            "🍷 Sebutkan judulnya agar aku bisa mencarikannya.\nContoh: <code>/play Frank Sinatra - Fly Me To The Moon</code>",
            parse_mode=ParseMode.HTML,
        )
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=20)
        return

    query = parts[1].strip()[:120]

    await player.react_msg(chat_id, message.message_id, "🍷")
    search_msg = await message.reply(
        f"🔎 <i>Sebentar, aku carikan piringan hitam untuk <b>{html.escape(query)}</b>...</i>",
        parse_mode=ParseMode.HTML,
    )

    ok = await player.enqueue_song_request(
        chat_id=chat_id,
        query=query,
        uid=user.id,
        requester_name=user.first_name,
        thread_id=message.message_thread_id,
    )
    player.schedule_delete(chat_id, [message.message_id, search_msg.message_id], delay=5 if ok else 15)


@router.message(Command("np", "nowplaying"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_now_playing(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await reject_unauthorized_group(message.bot, chat_id)
        return

    st = get_group_state(chat_id)
    if not st.current_track:
        err = await message.reply(
            "🕯️ Belum ada lagu yang berputar saat ini. Ketik <code>/play judul lagu</code> untuk memulai.",
            parse_mode=ParseMode.HTML,
        )
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    if st.now_playing_msg_id:
        try:
            await message.bot.delete_message(chat_id, st.now_playing_msg_id)
        except Exception:
            pass

    msg = await message.reply(
        player.format_now_playing_text(st),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=player.build_player_keyboard(is_paused=st.is_paused),
    )
    st.now_playing_msg_id = msg.message_id
    player.schedule_delete(chat_id, [message.message_id], delay=5)


@router.message(Command("queue"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_music_queue(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await reject_unauthorized_group(message.bot, chat_id)
        return

    st = get_group_state(chat_id)
    if not st.current_track and not st.queue:
        err = await message.reply("📭 Daftar putar malam ini masih kosong.")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    lines = []
    if st.current_track:
        lines.append(
            f"🎶 <b>Sedang Mengalun:</b> {html.escape(st.current_track.title)} "
            f"(<i>{html.escape(st.current_track.requester_name)}</i>)"
        )
    for idx, t in enumerate(list(st.queue)[:10], start=1):
        lines.append(f"{idx}. {html.escape(t.title)} — <i>{html.escape(t.requester_name)}</i>")

    msg = await message.reply(
        "📜 <b>Daftar Putar The Velvet Lounge</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )
    player.schedule_delete(chat_id, [message.message_id, msg.message_id], delay=40)


@router.message(Command("skip"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_skip_song(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await reject_unauthorized_group(message.bot, chat_id)
        return

    st = get_group_state(chat_id)
    if not st.current_track:
        err = await message.reply("🕯️ Tidak ada lagu yang sedang diputar untuk dilewati.")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    uid = message.from_user.id
    if uid != st.current_track.requester_id and not await is_user_admin_or_owner(chat_id, uid):
        err = await message.reply("⛔ Hanya pemesan lagu ini atau Admin yang berhak mengganti trek.")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    skipped_title = st.current_track.title
    await player.react_msg(chat_id, message.message_id, "⏭️")
    msg = await message.reply(
        f"⏭️ Baiklah, aku lewati <b>{html.escape(skipped_title)}</b>.",
        parse_mode=ParseMode.HTML,
    )
    player.schedule_delete(chat_id, [message.message_id, msg.message_id], delay=20)
    await player.play_next_in_queue(chat_id)


@router.message(Command("pause"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_pause_song(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await reject_unauthorized_group(message.bot, chat_id)
        return

    st = get_group_state(chat_id)
    if not st.current_track or st.is_paused:
        return

    uid = message.from_user.id
    if uid != st.current_track.requester_id and not await is_user_admin_or_owner(chat_id, uid):
        err = await message.reply("⛔ Hanya pemesan lagu ini atau Admin yang bisa menjeda musik.")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    try:
        await player.call_py_ref.pause(chat_id)
        st.is_paused = True
        if st.now_playing_msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=st.now_playing_msg_id,
                    text=player.format_now_playing_text(st),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=player.build_player_keyboard(is_paused=True),
                )
            except Exception:
                pass
        msg = await message.reply("⏸️ Alunan musik aku jeda sejenak.")
        player.schedule_delete(chat_id, [message.message_id, msg.message_id], delay=15)
    except Exception as e:
        err = await message.reply(f"Gagal menjeda: {e}")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)


@router.message(Command("resume"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_resume_song(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await reject_unauthorized_group(message.bot, chat_id)
        return

    st = get_group_state(chat_id)
    if not st.current_track or not st.is_paused:
        return

    uid = message.from_user.id
    if uid != st.current_track.requester_id and not await is_user_admin_or_owner(chat_id, uid):
        err = await message.reply("⛔ Hanya pemesan lagu ini atau Admin yang bisa melanjutkan musik.")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    try:
        await player.call_py_ref.resume(chat_id)
        st.is_paused = False
        if st.now_playing_msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=st.now_playing_msg_id,
                    text=player.format_now_playing_text(st),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=player.build_player_keyboard(is_paused=False),
                )
            except Exception:
                pass
        msg = await message.reply("▶️ Melanjutkan kembali melodi yang tertunda.")
        player.schedule_delete(chat_id, [message.message_id, msg.message_id], delay=15)
    except Exception as e:
        err = await message.reply(f"Gagal melanjutkan: {e}")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)


@router.message(Command("stop"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_stop_music(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await reject_unauthorized_group(message.bot, chat_id)
        return

    st = get_group_state(chat_id)
    uid = message.from_user.id
    if not await is_user_admin_or_owner(chat_id, uid):
        err = await message.reply(
            "⛔ Perintah <code>/stop</code> hanya tersedia untuk Admin atau Owner.",
            parse_mode=ParseMode.HTML,
        )
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    if st.now_playing_msg_id:
        try:
            await message.bot.delete_message(chat_id, st.now_playing_msg_id)
        except Exception:
            pass
        st.now_playing_msg_id = None

    st.queue.clear()
    st.current_track = None
    st.is_paused = False
    st.idle_since = time.monotonic()

    try:
        await player.call_py_ref.leave_call(chat_id)
    except Exception:
        pass

    msg = await message.reply("⏹️ Piringan hitam dihentikan dan seluruh antrean telah dibersihkan.")
    player.schedule_delete(chat_id, [message.message_id, msg.message_id], delay=25)
```[cite: 1]

---

### 10. `kaelen_dj/main.py`
```python
import asyncio
from pyrogram import Client
from pytgcalls import PyTgCalls, filters as tgcalls_filters
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

from kaelen_dj.config import BOT_TOKEN, API_ID, API_HASH, SESSION_STRING, ALLOWED_GROUPS, MUSIC_VAULT_ID, logger
from kaelen_dj.state import spawn_task
from kaelen_dj.os_guard import init_os_guard, enforce_os_listener_mute, periodic_os_mute_enforcer
from kaelen_dj.music_finder import init_finder
from kaelen_dj.player import init_player, on_tgcalls_stream_ended
from kaelen_dj.handlers import router


async def register_commands(bot: Bot):
    group_cmds = [
        BotCommand(command="start", description="Sapa Kaelen & cek status booth musik"),
        BotCommand(command="play", description="Putar lagu di Obrolan Suara"),
        BotCommand(command="np", description="Lihat & kontrol lagu yang sedang diputar"),
        BotCommand(command="queue", description="Lihat daftar antrean lagu"),
        BotCommand(command="skip", description="Lewati lagu saat ini"),
        BotCommand(command="pause", description="Jeda musik di OS"),
        BotCommand(command="resume", description="Lanjutkan musik yang dijeda"),
        BotCommand(command="stop", description="[Admin] Hentikan musik & bersihkan antrean"),
        BotCommand(command="feedback", description="Kirim masukan atau laporan ke pengelola"),
    ]
    private_cmds = [
        BotCommand(command="start", description="Mulai percakapan & lihat panduan"),
        BotCommand(command="help", description="Panduan lengkap perintah DJ Lounge"),
        BotCommand(command="feedback", description="Kirim saran atau laporan bug"),
    ]
    await bot.set_my_commands(group_cmds, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(private_cmds, scope=BotCommandScopeAllPrivateChats())


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await register_commands(bot)

    assistant = Client(
        name="music_assistant",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
    )
    assistant.on_raw_update()(enforce_os_listener_mute)

    call_py = PyTgCalls(assistant)
    call_py.on_update(tgcalls_filters.stream_end())(on_tgcalls_stream_ended)

    await assistant.start()
    me = await assistant.get_me()
    await call_py.start()

    init_os_guard(bot, assistant, call_py, me.id)
    init_finder(assistant)
    init_player(bot, call_py)

    logger.info(
        "Akun Asisten Multi-Group (@%s / %s) aktif! ALLOWED_GROUPS: %s | MUSIC_VAULT_ID: %s",
        me.username or me.first_name,
        me.id,
        sorted(ALLOWED_GROUPS) if ALLOWED_GROUPS else "KOSONG (Semua grup diblokir)",
        MUSIC_VAULT_ID,
    )

    spawn_task(periodic_os_mute_enforcer())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
```[cite: 1]

---

### Cara Menjalankan Modul:
Gunakan perintah terminal yang Anda inginkan untuk menjalankan aplikasi:
```bash
python -m kaelen_dj.main start
