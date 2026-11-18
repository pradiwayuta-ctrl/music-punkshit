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
