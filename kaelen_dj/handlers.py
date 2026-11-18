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
            "🖕 <i>Sori bray, tongkrongan ini kagak masuk daftar tamu gue. Gue cabut dulu!</i>",
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
                InlineKeyboardButton(text="🎸 Cara Nge-Gig", callback_data="pc:tutor"),
                InlineKeyboardButton(text="🎛 Daftar Perintah", callback_data="pc:cmds"),
            ],
            [
                InlineKeyboardButton(text="📣 Bacotan / Feedback", callback_data="pc:feedback"),
            ],
        ]
    )


def _group_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎛 Panel Player", callback_data="grp:panel"),
                InlineKeyboardButton(text="📜 Cek Antrean", callback_data="grp:queue"),
            ],
            [
                InlineKeyboardButton(text="🎸 Cara Req Lagu", callback_data="grp:howto"),
                InlineKeyboardButton(text="📣 Lapor / Feedback", callback_data="grp:fb"),
            ],
        ]
    )


# --- MENU BUTTON DI PRIVATE CHAT (PC BOT) ---
@router.message(Command("start", "help"), F.chat.type == "private")
async def cmd_start_private(message: types.Message):
    user_mention = player.mention(message.from_user.id, message.from_user.first_name)
    text = (
        f"🤘 <b>Yo, apa kabar {user_mention}!</b>\n\n"
        f"Gue <b>Kaelen</b>, operator sound system di <i>Underground Stage</i>. "
        f"Kerjaan gue bikin tongkrongan lo berisik pake musik pilihan anak-anak skena—anti ribet, anti aturan kaku.\n\n"
        f"<i>Pencet aja tombol di bawah buat liat cara main atau kirim bacotan ke bos besar:</i>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=_private_menu_keyboard())


@router.callback_query(F.data.startswith("pc:"))
async def on_private_menu_click(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    back_btn = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Balik ke Depan", callback_data="pc:home")]]
    )

    if action == "tutor":
        tutor_text = (
            "🎸 <b>Panduan Nge-Gig di Grup</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ <b>Beresin Pangkat:</b>\n"
            "   • Angkat bot & akun asisten jadi <b>Admin</b> di grup (wajib centang <i>Manage Voice Chats</i> & <i>Invite Users</i>).\n\n"
            "2️⃣ <b>Putar Distorsi:</b>\n"
            "   • Ketik: <code>/play Green Day - American Idiot</code>\n"
            "   • Atau: <code>/play Blink-182 - Dammit</code>\n"
            "   • OS grup bakal kebuka otomatis & pendengar biasa langsung di-mute biar kaga berisik!\n\n"
            "3️⃣ <b>Kendali Penuh Pake Tombol:</b>\n"
            "   • Tinggal pencet tombol <b>⏸ Rem Dulu</b>, <b>▶️ Gas Lagi</b>, <b>⏭ Ganti Trek</b>, atau <b>⏹ Cabut Kabel</b> di player."
        )
        await callback.message.edit_text(tutor_text, parse_mode=ParseMode.HTML, reply_markup=back_btn)

    elif action == "cmds":
        cmds_text = (
            "🎛 <b>Daftar Perintah Tongkrongan</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "• <code>/play judul lagu</code> — Gas putar / masukin antrean\n"
            "• <code>/np</code> — Munculin tombol kontrol player\n"
            "• <code>/queue</code> — Liat antrean moshpit\n"
            "• <code>/skip</code> — Tendang lagu yang lagi jalan\n"
            "• <code>/pause</code> & <code>/resume</code> — Rem atau gas lagi\n"
            "• <code>/stop</code> — <i>[Admin]</i> Matiin ampli & bubarin antrean\n"
            "• <code>/feedback pesan</code> — Kirim keluhan/request ke owner"
        )
        await callback.message.edit_text(cmds_text, parse_mode=ParseMode.HTML, reply_markup=back_btn)

    elif action == "feedback":
        fb_text = (
            "📣 <b>Kirim Bacotan / Feedback</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Ada bug, lagu nyangkut, atau mau request fitur gokil?\n\n"
            "Ketik aja langsung format ini:\n"
            "<code>/feedback isi pesan lo di sini</code>\n\n"
            "<i>Contoh:</i> <code>/feedback Tambahin playlist punk rock 90an dong bray!</code>"
        )
        await callback.message.edit_text(fb_text, parse_mode=ParseMode.HTML, reply_markup=back_btn)

    elif action == "home":
        user_mention = player.mention(callback.from_user.id, callback.from_user.first_name)
        await callback.message.edit_text(
            f"🤘 <b>Balik lagi, {user_mention}!</b>\n"
            f"Pilih tombol di bawah mau ngapain:",
            parse_mode=ParseMode.HTML,
            reply_markup=_private_menu_keyboard(),
        )
    await callback.answer()


@router.message(Command("feedback"))
async def cmd_feedback(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        err = await message.reply(
            "📣 Tulis dulu bacotan lo setelah command, bray!\nContoh: <code>/feedback Suara lagu putus-putus nih</code>",
            parse_mode=ParseMode.HTML,
        )
        if message.chat.type != "private":
            player.schedule_delete(message.chat.id, [message.message_id, err.message_id], delay=20)
        return

    fb_content = html.escape(parts[1].strip()[:1000])
    sender = player.mention(message.from_user.id, message.from_user.first_name)
    origin = (
        "Private Chat"
        if message.chat.type == "private"
        else f"Grup <b>{html.escape(message.chat.title or str(message.chat.id))}</b> (<code>{message.chat.id}</code>)"
    )

    try:
        await message.bot.send_message(
            OWNER_ID,
            f"📣 <b>Surat Kaleng Baru — Kaelen DJ</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Dari:</b> {sender} (<code>{message.from_user.id}</code>)\n"
            f"• <b>TKP:</b> {origin}\n"
            f"• <b>Isi Bacotan:</b>\n<blockquote>{fb_content}</blockquote>",
            parse_mode=ParseMode.HTML,
        )
        reply_msg = await message.reply(
            "🤘 <i>Sip! Pesan lo udah gue lempar ke meja bos besar.</i>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        reply_msg = await message.reply("💥 Gagal ngirim pesan, jaringan lagi ampas.")

    if message.chat.type != "private":
        player.schedule_delete(message.chat.id, [message.message_id, reply_msg.message_id], delay=20)


# --- GREETING & MENU BUTTON SAAT BOT MASUK GRUP ---
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
            "✅ Akun asisten udah nongkrong di pojokan, siap ngegas."
            if joined
            else "⚠️ <i>Kasih gue pangkat <b>Admin</b> + izin <b>Invite Users</b> & <b>Manage Voice Chats</b> biar asisten gue bisa masuk OS!</i>"
        )
        await player.send_group_msg(
            chat_id,
            f"🤘 <b>Panggung udah kebuka di {html.escape(event.chat.title or 'tongkrongan ini')}!</b>\n"
            f"{status_note}\n\n"
            f"<i>Pencet tombol di bawah buat cek menu & cara main:</i>",
            auto_delete=60,
            reply_markup=_group_menu_keyboard(),
        )


@router.message(Command("start", "help"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_start_group(message: types.Message):
    chat_id = message.chat.id
    if not is_group_allowed(chat_id):
        await reject_unauthorized_group(message.bot, chat_id)
        return

    user_mention = player.mention(message.from_user.id, message.from_user.first_name)
    text = (
        f"🤘 <b>Yo {user_mention}, sound system udah nyala!</b>\n"
        f"Mau ngapain nih? Pencet aja tombol di bawah:"
    )
    sent = await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=_group_menu_keyboard())
    player.schedule_delete(chat_id, [message.message_id, sent.message_id], delay=45)


# --- CALLBACK MENU TOMBOL DI GRUP ---
@router.callback_query(F.data.startswith("grp:"))
async def on_group_menu_click(callback: CallbackQuery):
    if not callback.message or callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    if not is_group_allowed(chat_id):
        await callback.answer("🖕 Tongkrongan ini kagak terdaftar!", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    st = get_group_state(chat_id)
    back_grp = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Balik ke Menu", callback_data="grp:home")]]
    )

    if action == "home":
        await callback.message.edit_text(
            "🤘 <b>Markas Sound System — Pilih Tombol:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_group_menu_keyboard(),
        )
        await callback.answer()

    elif action == "howto":
        await callback.message.edit_text(
            "🎸 <b>Cara Request Lagu di Sini</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Ketik <code>/play judul lagu - band</code>\n"
            "• Contoh: <code>/play Superman Is Dead - Kuat Kita Bersinar</code>\n"
            "• Contoh: <code>/play Tipe-X - Genit</code>\n\n"
            "<i>Abis itu tinggal pencet-pencet tombol di panel player buat pause, skip, atau cek antrean!</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_grp,
        )
        await callback.answer()

    elif action == "fb":
        await callback.message.edit_text(
            "📣 <b>Kirim Laporan / Request Fitur</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Ketik langsung di chat:\n"
            "<code>/feedback pesan lo di sini</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_grp,
        )
        await callback.answer()

    elif action == "queue":
        if not st.current_track and not st.queue:
            await callback.answer("📭 Antrean masih kosong melompong, bray!", show_alert=True)
            return
        lines = []
        if st.current_track:
            lines.append(f"🔥 {st.current_track.title[:38]}")
        for idx, t in enumerate(list(st.queue)[:6], start=1):
            lines.append(f"{idx}. {t.title[:35]}")
        if len(st.queue) > 6:
            lines.append(f"... +{len(st.queue) - 6} lagu lagi")
        await callback.answer("\n".join(lines)[:200], show_alert=True)

    elif action == "panel":
        if not st.current_track:
            await callback.answer("🎸 Belum ada lagu yang ngegas! Ketik /play dulu.", show_alert=True)
            return
        await callback.message.edit_text(
            player.format_now_playing_text(st),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=player.build_player_keyboard(is_paused=st.is_paused),
        )
        st.now_playing_msg_id = callback.message.message_id
        await callback.answer("🎛 Panel player dibuka!")


# --- CALLBACK HANDLER UNTUK TOMBOL INLINE PLAYER ---
@router.callback_query(F.data.startswith("dj:"))
async def on_player_button_click(callback: CallbackQuery):
    if not callback.message or callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    if not is_group_allowed(chat_id):
        await callback.answer("🖕 Tongkrongan ini kagak terdaftar!", show_alert=True)
        return

    st = get_group_state(chat_id)
    uid = callback.from_user.id
    action = callback.data.split(":", 1)[1]

    if action == "queue":
        if not st.current_track and not st.queue:
            await callback.answer("📭 Antrean moshpit masih kosong, bray!", show_alert=True)
            return
        lines = []
        if st.current_track:
            lines.append(f"🔥 {st.current_track.title[:38]}")
        for idx, t in enumerate(list(st.queue)[:6], start=1):
            lines.append(f"{idx}. {t.title[:35]}")
        if len(st.queue) > 6:
            lines.append(f"... +{len(st.queue) - 6} lagu lagi")
        await callback.answer("\n".join(lines)[:200], show_alert=True)
        return

    if action == "panel":
        if not st.current_track:
            await callback.answer("🎸 Kagak ada lagu yang lagi jalan.", show_alert=True)
            return
        if st.now_playing_msg_id:
            try:
                await callback.message.bot.delete_message(chat_id, st.now_playing_msg_id)
            except Exception:
                pass
        sent = await player.send_group_msg(
            chat_id,
            player.format_now_playing_text(st),
            thread_id=callback.message.message_thread_id,
            auto_delete=0,
            reply_markup=player.build_player_keyboard(is_paused=st.is_paused),
        )
        if sent:
            st.now_playing_msg_id = sent.message_id
        await callback.answer("🎛 Panel player muncul!")
        return

    if not st.current_track:
        await callback.answer("🎸 Ampli lagi mati, kaga ada lagu yang jalan.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    if action == "refresh":
        try:
            await callback.message.edit_text(
                player.format_now_playing_text(st),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=player.build_player_keyboard(is_paused=st.is_paused),
            )
        except Exception:
            pass
        await callback.answer("🔄 Panel udah seger lagi!")
        return

    is_admin = await is_user_admin_or_owner(chat_id, uid)
    is_requester = uid == st.current_track.requester_id

    if action == "stop":
        if not is_admin:
            await callback.answer("⛔ Eits, cuma Admin yang boleh cabut kabel ampli!", show_alert=True)
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
        await callback.answer("⏹️ Kabel ampli dicabut!")
        try:
            await callback.message.edit_text(
                f"⏹️ Kabel ampli dicabut sama {player.mention(uid, callback.from_user.first_name)}. Tongkrongan bubar!",
                parse_mode=ParseMode.HTML,
            )
            player.schedule_delete(chat_id, [callback.message.message_id], delay=15)
        except Exception:
            pass
        return

    if not (is_admin or is_requester):
        await callback.answer("⛔ Jangan asal pencet! Cuma yang req lagu atau Admin yang pegang kendali.", show_alert=True)
        return

    if action == "pause":
        if st.is_paused:
            await callback.answer("Udah di-rem dari tadi, bray.")
            return
        try:
            await player.call_py_ref.pause(chat_id)
            st.is_paused = True
            await callback.answer("⏸️ Musik di-rem bentar.")
            await callback.message.edit_text(
                player.format_now_playing_text(st),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=player.build_player_keyboard(is_paused=True),
            )
        except Exception as e:
            await callback.answer(f"Gagal nge-rem: {e}", show_alert=True)

    elif action == "resume":
        if not st.is_paused:
            await callback.answer("Lagi ngegas kok ini!")
            return
        try:
            await player.call_py_ref.resume(chat_id)
            st.is_paused = False
            await callback.answer("▶️ Gas pol lagi!")
            await callback.message.edit_text(
                player.format_now_playing_text(st),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=player.build_player_keyboard(is_paused=False),
            )
        except Exception as e:
            await callback.answer(f"Gagal ngegas: {e}", show_alert=True)

    elif action == "skip":
        skipped_title = st.current_track.title
        await callback.answer(f"⏭️ Nendang lagu: {skipped_title[:40]}")
        await player.send_group_msg(
            chat_id,
            f"⏭️ <b>{html.escape(skipped_title)}</b> ditendang dari panggung sama {player.mention(uid, callback.from_user.first_name)}.",
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
            "🤘 Sebut judul lagunya dong bray!\nContoh: <code>/play Green Day - Basket Case</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_group_menu_keyboard(),
        )
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=20)
        return

    query = parts[1].strip()[:120]

    await player.react_msg(chat_id, message.message_id, "🤘")
    search_msg = await message.reply(
        f"🔎 <i>Bentar, lagi ngubek kaset <b>{html.escape(query)}</b>...</i>",
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
            "🎸 Belum ada lagu yang ngegas, bray. Ketik <code>/play judul lagu</code> atau pencet tombol di bawah:",
            parse_mode=ParseMode.HTML,
            reply_markup=_group_menu_keyboard(),
        )
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=20)
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
        err = await message.reply("📭 Antrean moshpit masih kosong melompong.")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    lines = []
    if st.current_track:
        lines.append(
            f"🔥 <b>Lagi Ngegas:</b> {html.escape(st.current_track.title)} "
            f"(<i>{html.escape(st.current_track.requester_name)}</i>)"
        )
    for idx, t in enumerate(list(st.queue)[:10], start=1):
        lines.append(f"{idx}. {html.escape(t.title)} — <i>{html.escape(t.requester_name)}</i>")

    msg = await message.reply(
        "📜 <b>Antrean Moshpit — Underground Stage</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🎛 Buka Panel Player", callback_data="dj:panel")]]
        ),
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
        err = await message.reply("🎸 Kagak ada lagu yang lagi jalan buat di-skip.")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    uid = message.from_user.id
    if uid != st.current_track.requester_id and not await is_user_admin_or_owner(chat_id, uid):
        err = await message.reply("⛔ Cuma yang req lagu ini atau Admin yang boleh nendang trek.")
        player.schedule_delete(chat_id, [message.message_id, err.message_id], delay=15)
        return

    skipped_title = st.current_track.title
    await player.react_msg(chat_id, message.message_id, "⏭️")
    msg = await message.reply(
        f"⏭️ Sip, <b>{html.escape(skipped_title)}</b> ditendang dari panggung!",
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
        err = await message.reply("⛔ Cuma yang req lagu ini atau Admin yang bisa nge-rem musik.")
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
        msg = await message.reply("⏸️ Musik di-rem bentar.")
        player.schedule_delete(chat_id, [message.message_id, msg.message_id], delay=15)
    except Exception as e:
        err = await message.reply(f"Gagal nge-rem: {e}")
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
        err = await message.reply("⛔ Cuma yang req lagu ini atau Admin yang bisa ngegas lagi.")
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
        msg = await message.reply("▶️ Gas pol lagi!")
        player.schedule_delete(chat_id, [message.message_id, msg.message_id], delay=15)
    except Exception as e:
        err = await message.reply(f"Gagal ngegas: {e}")
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
            "⛔ Perintah <code>/stop</code> cuma buat Admin atau Owner tongkrongan.",
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

    msg = await message.reply("⏹️ Kabel ampli dicabut & seluruh antrean udah dibersihin!")
    player.schedule_delete(chat_id, [message.message_id, msg.message_id], delay=25)
