import asyncio
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

# ===== ЧТЕНИЕ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
MOD_CHAT_ID_RAW = os.getenv("MOD_CHAT_ID")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")

print("ENV BOT_TOKEN:", BOT_TOKEN)
print("ENV MOD_CHAT_ID:", MOD_CHAT_ID_RAW)
print("ENV CHANNEL_ID:", CHANNEL_ID_RAW)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения")

if not CHANNEL_ID_RAW:
    raise ValueError("CHANNEL_ID не задан в переменных окружения")

CHANNEL_ID = int(CHANNEL_ID_RAW)

if MOD_CHAT_ID_RAW:
    MOD_CHAT_ID = int(MOD_CHAT_ID_RAW)
else:
    MOD_CHAT_ID = None
    print("WARNING: MOD_CHAT_ID не задан, модерация отключена")


# ===== ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА =====
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher(storage=MemoryStorage())


# ===== ХЕНДЛЕРЫ =====
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Отправь мне свою историю текстом, я передам её на модерацию."
    )


@dp.message()
async def handle_story(message: Message):
    # Текст истории
    story_text = message.text

    # Сообщение пользователю
    await message.answer("История отправлена на модерацию ✅")

    # Если модерационный чат настроен — отправляем туда
    if MOD_CHAT_ID:
        text = (
            f"🆕 Новая история\n"
            f"От: @{message.from_user.username or 'без_ника'} (id {message.from_user.id})\n\n"
            f"{story_text}"
        )
        await bot.send_message(MOD_CHAT_ID, text)
    else:
        # Если нет MOD_CHAT_ID, просто логируем
        print("SKIP: нет MOD_CHAT_ID, историю некому отправить на модерацию")


# ===== ТОЧКА ВХОДА =====
async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
