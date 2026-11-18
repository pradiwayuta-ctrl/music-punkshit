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
            "🎸 <i>Udah 5 menit kaga ada yang ngegas lagu. Ampli gue matiin dulu ya bray—panggil lagi kalo mau berisik!</i>",
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
