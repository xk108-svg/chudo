import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
import aiohttp

# ---------- МОДЕЛЬ ИСТОРИИ ----------

@dataclass
class Story:
    id: Optional[int]
    user_id: int
    username: str
    text: str
    status: str = "pending"


# ---------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ----------

BOT_TOKEN = os.getenv("BOT_TOKEN")
MOD_CHAT_ID_RAW = os.getenv("MOD_CHAT_ID")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print("ENV BOT_TOKEN:", BOT_TOKEN)
print("ENV MOD_CHAT_ID:", MOD_CHAT_ID_RAW)
print("ENV CHANNEL_ID:", CHANNEL_ID_RAW)
print("ENV SUPABASE_URL:", SUPABASE_URL)
print("ENV SUPABASE_KEY set:", bool(SUPABASE_KEY))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

if not CHANNEL_ID_RAW:
    raise ValueError("CHANNEL_ID не задан")

CHANNEL_ID = int(CHANNEL_ID_RAW)

if MOD_CHAT_ID_RAW:
    MOD_CHAT_ID = int(MOD_CHAT_ID_RAW)
else:
    MOD_CHAT_ID = None
    print("WARNING: MOD_CHAT_ID не задан — модерация в чат отключена")

SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)


# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ SUPABASE ----------

async def supabase_request(method: str, path: str, json: Optional[dict] = None, params: Optional[dict] = None):
    if not SUPABASE_ENABLED:
        return None

    url = f"{SUPABASE_URL}{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if method.upper() in ("POST", "PATCH", "DELETE"):
        headers["Prefer"] = "return=representation"

    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=json, params=params) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                print(f"Supabase error {resp.status}: {data}")
                return None
            return data


async def save_story_to_supabase(story: Story) -> Optional[int]:
    """
    Сохраняет историю, возвращает ID записи или None.
    """
    if not SUPABASE_ENABLED:
        return None

    payload = {
        "user_id": story.user_id,
        "username": story.username,
        "story": story.text,
        "status": story.status,
    }
    data = await supabase_request("POST", "/rest/v1/stories", json=payload)
    if not data:
        return None

    # Supabase вернет список вставленных строк
    try:
        return data[0]["id"]
    except Exception as e:
        print("Parse Supabase insert response error:", e, data)
        return None


async def delete_story_from_supabase(story_id: int) -> bool:
    if not SUPABASE_ENABLED:
        return False

    params = {"id": f"eq.{story_id}"}
    data = await supabase_request("DELETE", "/rest/v1/stories", params=params)
    return data is not None


# ---------- КНОПКИ МОДЕРАЦИИ ----------

def moderation_keyboard(story_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"approve:{story_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{story_id}",
                ),
            ]
        ]
    )


# ---------- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ----------

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 📝 Отправь свою историю одним сообщением.\n"
        "Я передам её на модерацию и, если все ок, опубликую в канале."
    )


@router.message(F.text.startswith("/ad "))
async def cmd_ad(message: Message):
    """
    Твоя команда для рекламы: в личке/модерационном чате пишешь
    /ad текст рекламы
    и бот публикует это в канал.
    """
    ad_text = message.text[4:].strip()
    if not ad_text:
        await message.answer("После /ad напиши текст объявления.")
        return

    await bot.send_message(CHANNEL_ID, f"📢 Реклама:\n\n{ad_text}")
    await message.answer("Рекламное сообщение опубликовано в канале ✅")


@router.message()
async def handle_story(message: Message):
    user = message.from_user
    story_text = message.text

    story = Story(
        id=None,
        user_id=user.id,
        username=user.username or "anon",
        text=story_text,
    )

    # сохраняем в Supabase
    story_id = await save_story_to_supabase(story)
    story.id = story_id

    await message.answer("История отправлена на модерацию ✅")

    # отправляем модераторам
    if MOD_CHAT_ID:
        supabase_mark = (
            f"ID в БД: {story_id}" if story_id is not None else "❌ не сохранилась в БД"
        )
        text = (
            f"🆕 Новая история\n"
            f"Автор: @{story.username} (id {story.user_id})\n"
            f"{supabase_mark}\n\n"
            f"{story.text}"
        )

        kb = moderation_keyboard(story_id or 0)
        await bot.send_message(MOD_CHAT_ID, text, reply_markup=kb)
    else:
        print("SKIP: нет MOD_CHAT_ID, модераторам не отправлено")


# ---------- ХЕНДЛЕРЫ КНОПОК МОДЕРАЦИИ ----------

@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery):
    await call.answer()

    payload = call.data.split(":", 1)[1]
    try:
        story_id = int(payload)
    except ValueError:
        await call.message.answer("Ошибка: некорректный ID истории.")
        return

    # текст истории = текст сообщения без первой строки/метаданных
    # предполагаем формат как выше
    full_text = call.message.text or ""
    # можно сразу публиковать всё сообщение или только часть после метаданных
    # упрощённо публикуем всё, кроме первых трёх строк:
    lines = full_text.split("\n")
    if len(lines) > 3:
        story_text = "\n".join(lines[3:])
    else:
        story_text = full_text

    # публикуем в канал
    await bot.send_message(CHANNEL_ID, story_text)

    # удаляем из Supabase, если есть id
    if story_id != 0:
        deleted = await delete_story_from_supabase(story_id)
        print("Supabase delete:", deleted)

    # отмечаем в модерационном чате
    await call.message.edit_text(full_text + "\n\n✅ Одобрено и опубликовано.")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    await call.answer()

    payload = call.data.split(":", 1)[1]
    try:
        story_id = int(payload)
    except ValueError:
        story_id = 0

    if story_id != 0:
        deleted = await delete_story_from_supabase(story_id)
        print("Supabase delete (reject):", deleted)

    full_text = call.message.text or ""
    await call.message.edit_text(full_text + "\n\n❌ Отклонено.")


# ---------- ЗАПУСК ----------

async def main():
    print("Bot started polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
