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
