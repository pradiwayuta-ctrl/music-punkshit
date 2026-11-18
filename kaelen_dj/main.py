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
        BotCommand(command="start", description="Nyalain sound system & buka menu tombol"),
        BotCommand(command="play", description="Putar lagu / masukin antrean moshpit"),
        BotCommand(command="np", description="Buka panel kontrol lagu yang lagi ngegas"),
        BotCommand(command="queue", description="Cek daftar antrean lagu"),
        BotCommand(command="skip", description="Tendang lagu yang lagi jalan"),
        BotCommand(command="pause", description="Rem musik bentar"),
        BotCommand(command="resume", description="Gas pol musik lagi"),
        BotCommand(command="stop", description="[Admin] Cabut kabel ampli & bubarin antrean"),
        BotCommand(command="feedback", description="Kirim bacotan atau laporan ke bos besar"),
    ]
    private_cmds = [
        BotCommand(command="start", description="Buka markas & menu tombol"),
        BotCommand(command="help", description="Panduan nge-gig di grup"),
        BotCommand(command="feedback", description="Kirim keluhan atau request fitur"),
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
    # PERBAIKAN: Hapus tanda kurung () pada stream_end
    call_py.on_update(tgcalls_filters.stream_end)(on_tgcalls_stream_ended)

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
