import html
import time
from aiogram import F, types, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated, ReactionTypeEmoji
from aiogram.enums import ParseMode

from config import AUTO_DELETE_DELAY
from state import get_group_state, is_group_allowed
from services import resolve_audio_track, ensure_voice_chat_open, call_py, assistant

router = Router()

def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name or "Tamu")}</a>'

def build_player_keyboard(is_paused: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Lanjutkan" if is_paused else "⏸ Jeda", callback_data="dj:resume" if is_paused else "dj:pause"),
                InlineKeyboardButton(text="⏭ Lewati", callback_data="dj:skip"),
                InlineKeyboardButton(text="⏹ Hentikan", callback_data="dj:stop"),
            ],
            [InlineKeyboardButton(text="📜 Daftar Antrean", callback_data="dj:queue")],
        ]
    )

def format_now_playing(st) -> str:
    track = st.current_track
    if not track:
        return "🕯️ <i>Ruangan sedang hening. Belum ada piringan hitam yang berputar.</i>"
    state_label = "⏸ Dijeda" if st.is_paused else "🎶 Sedang Mengalun"
    return (
        f"🎧 <b>Kaelen's Lounge — {state_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Trek:</b> <b>{html.escape(track.title)}</b> (<code>{track.duration}</code>)\n"
        f"• <b>Pilihan:</b> {mention(track.requester_id, track.requester_name)}\n"
        f"• <b>Antrean:</b> {len(st.queue)} lagu"
    )

@router.message(Command("start", "help"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_start_group(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await message.reply("⛔ Ruangan ini tidak diizinkan menggunakan bot ini.")
        return
    await message.reply("🍷 <b>Booth musik siap.</b> Ketik <code>/play judul lagu</code> untuk mulai memutar musik.", parse_mode=ParseMode.HTML)

@router.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def on_bot_added(event: ChatMemberUpdated):
    chat_id = event.chat.id
    if event.new_chat_member.status in ("member", "administrator"):
        if not is_group_allowed(chat_id):
            try:
                await event.bot.send_message(chat_id, "Maaf, ruangan ini tidak terdaftar. Saya undur diri.")
                await event.bot.leave_chat(chat_id)
            except Exception:
                pass
            return
        from services import ensure_assistant_in_group
        await ensure_assistant_in_group(chat_id, event.bot)
        await event.bot.send_message(chat_id, "🍷 <b>Tirai lounge dibuka!</b> Asisten siap menemani.", parse_mode=ParseMode.HTML)

@router.message(Command("play"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_play(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Contoh: <code>/play Frank Sinatra</code>", parse_mode=ParseMode.HTML)
        return

    query = parts[1].strip()
    status_msg = await message.reply(f"🔎 Mencari <b>{html.escape(query)}</b>...", parse_mode=ParseMode.HTML)

    info = await resolve_audio_track(query)
    if not info:
        await status_msg.edit_text("❌ Lagu tidak ditemukan.")
        return

    st = get_group_state(chat_id)
    from state import TrackItem
    track = TrackItem(
        title=info["title"],
        stream_url=info["stream_url"],
        webpage_url=info["webpage_url"],
        duration=info["duration"],
        requester_id=message.from_user.id,
        requester_name=message.from_user.first_name,
        thread_id=message.message_thread_id,
        source_label=info["source_label"]
    )

    async with st.enqueue_lock:
        st.queue.append(track)
        should_play = st.current_track is None

    await status_msg.delete()

    if should_play:
        await play_next(chat_id, message.bot)
    else:
        await message.reply(f"💿 Ditambahkan ke antrean: <b>{html.escape(track.title)}</b>", parse_mode=ParseMode.HTML)

async def play_next(chat_id: int, bot_instance):
    st = get_group_state(chat_id)
    async with st.play_lock:
        if not st.queue:
            st.current_track = None
            try:
                await call_py.leave_call(chat_id)
            except Exception:
                pass
            return

        track = st.queue.popleft()
        st.current_track = track

        try:
            call = await ensure_voice_chat_open(chat_id, bot_instance)
            from pytgcalls.types import MediaStream, AudioQuality
            stream = MediaStream(track.stream_url, audio_parameters=AudioQuality.HIGH)
            await call_py.play(chat_id, stream)

            sent = await bot_instance.send_message(
                chat_id,
                format_now_playing(st),
                parse_mode=ParseMode.HTML,
                reply_markup=build_player_keyboard(False)
            )
            st.now_playing_msg_id = sent.message_id
        except Exception as e:
            st.current_track = None
            await bot_instance.send_message(chat_id, f"⚠️ Gagal memutar lagu: {e}")
