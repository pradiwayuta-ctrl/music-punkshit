import asyncio
import logging
from pyrogram import Client
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from pytgcalls import PyTgCalls

from config import BOT_TOKEN, API_ID, API_HASH, SESSION_STRING
from services import init_services
from handlers import router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kaelen_main")

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    assistant = Client(
        name="music_assistant",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
    )
    call_py = PyTgCalls(assistant)

    await assistant.start()
    me = await assistant.get_me()
    await call_py.start()

    init_services(assistant, call_py, me.id)
    logger.info("Bot Multi-Grup & Asisten Berhasil Dinyalakan!")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
