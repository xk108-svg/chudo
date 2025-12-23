import asyncio
import os
import time
import re
from dataclasses import dataclass
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
import aiohttp


# ---------- НАСТРОЙКИ ПРОЕКТА ----------

ADMIN_USER_ID = 318289611
LIMIT_SECONDS = 2 * 24 * 60 * 60

last_story_ts: Dict[int, float] = {}


# ---------- FSM СОСТОЯНИЯ ----------

class LongStory(StatesGroup):
    title = State()
    part1 = State()
    part2 = State()
    part3 = State()
    photo = State()


# ---------- МОДЕЛЬ ИСТОРИИ ----------

@dataclass
class Story:
    id: Optional[int]
    user_id: int
    username: str
    text: str
    status: str = "pending"
    type: str = "text"
    photo_file_id: Optional[str] = None


# ---------- ENV ----------

BOT_TOKEN = os.getenv("BOT_TOKEN")
MOD_CHAT_ID_RAW = os.getenv("MOD_CHAT_ID")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print("ENV:", {k: bool(v) for k, v in {
    "BOT_TOKEN": BOT_TOKEN,
    "CHANNEL_ID": CHANNEL_ID_RAW,
    "MOD_CHAT_ID": MOD_CHAT_ID_RAW,
    "SUPABASE": SUPABASE_URL,
}.items()})

CHANNEL_ID = int(CHANNEL_ID_RAW)
MOD_CHAT_ID = int(MOD_CHAT_ID_RAW) if MOD_CHAT_ID_RAW else None
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)


# ---------- Бот ----------

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ---------- SUPABASE ----------

async def save_story_to_supabase(story: Story) -> Optional[int]:
    if not SUPABASE_ENABLED: return None
    # ... как было ...
    return None  # заглушка

async def delete_story_from_supabase(story_id: int) -> bool:
    return False


# ---------- КНОПКИ ----------

def moderation_keyboard(story_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{story_id}"),
         InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{story_id}")]
    ])

def share_your_story_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✍️ Поделись своей историей", url="https://t.me/pishiistorii_bot")]
    ])


def extract_user_id_from_moderation_text(text: str) -> Optional[int]:
    m = re.search(r"\(id (\d+)\)", text)
    return int(m.group(1)) if m else None


# ---------- ПРИВЕТСТВИЕ ----------

START_MSGS = [
    "Добро пожаловать, путник истории...",
    "Перед тем как начать, давай позаботимся о чистоте речи...",
    "Пиши так, как будто рассказываешь историю перед алтарём...",
    "В конце истории ты можешь добавить до двух хештегов..."
]


# ---------- 🔥 ГЛАВНЫЙ ХЕНДЛЕР ----------

@router.message()
async def catch_all_handler(message: Message, state: FSMContext):
    """🎯 ЛОВИТ АБСОЛЮТНО ВСЁ!"""
    print(f"📨 MESSAGE: {message.from_user.id} | text: {bool(message.text)} | photo: {bool(message.photo)}")
    
    # 1. Команды
    if message.text == "/start":
        for msg in START_MSGS:
            await message.answer(msg)
        return
    if message.text and message.text.startswith("/ad "):
        await cmd_ad(message)
        return
    if message.text == "/long_story":
        await cmd_long_story(message, state)
        return
    if message.text == "/cancel":
        await cancel_long(message, state)
        return
    
    # 2. FSM (длинная история)
    current_state = await state.get_state()
    if current_state:
        if current_state == LongStory.title.state: await long_title(message, state)
        elif current_state == LongStory.part1.state: await long_part1(message, state)
        elif current_state == LongStory.part2.state: await long_part2(message, state)
        elif current_state == LongStory.part3.state: await long_part3(message, state)
        elif current_state == LongStory.photo.state: await long_photo(message, state)
        return
    
    # 3. ВСЁ ОСТАЛЬНОЕ = ИСТОРИЯ
    await process_story(message)


async def process_story(message: Message):
    """Обрабатывает ЛЮБУЮ историю"""
    user = message.from_user
    text = message.caption or message.text or ""
    has_photo = message.photo is not None
    photo_file_id = message.photo[-1].file_id if has_photo else None
    
    print(f"✅ PROCESS STORY: {user.id} | {len(text)} chars | photo: {has_photo}")
    
    # Лимит (кроме админа)
    if user.id != ADMIN_USER_ID:
        now = time.time()
        last_ts = last_story_ts.get(user.id)
        if last_ts and now - last_ts < LIMIT_SECONDS:
            hours_left = int((LIMIT_SECONDS - (now - last_ts)) // 3600) + 1
            await message.answer(f"⏳ Подожди {hours_left} ч.")
            return
        last_story_ts[user.id] = now
    
    # Сохраняем
    story = Story(
        user_id=user.id,
        username=user.username or "anon",
        text=text,
        type="photo" if has_photo else "text",
        photo_file_id=photo_file_id,
    )
    
    story_id = await save_story_to_supabase(story)
    await message.answer("✅ История отправлена на модерацию!")
    
    # Модерация
    if MOD_CHAT_ID:
        header = (
            f"🆕 История\n"
            f"Автор: @{story.username} (id {story.user_id})\n"
            f"📄 {len(text)} символов\n"
            f"{'📷 +фото' if has_photo else '📝 текст'}\n"
            f"ID: {story_id or 'нет'}\n\n"
        )
        kb = moderation_keyboard(story_id or 0)
        
        try:
            if has_photo:
                await bot.send_photo(MOD_CHAT_ID, photo=photo_file_id, caption=header + text, reply_markup=kb)
            else:
                await bot.send_message(MOD_CHAT_ID, header + text, reply_markup=kb)
            print(f"✅ SENT TO MOD!")
        except Exception as e:
            print(f"❌ MOD ERROR: {e}")


# ---------- /ad ----------

async def cmd_ad(message: Message):
    # ... как было ...
    pass


# ---------- Длинная история ----------

async def cmd_long_story(message: Message, state: FSMContext):
    if await state.get_state():
        await message.answer("⏳ Уже пишешь! /cancel")
        return
    print(f"🚀 LONG STORY: {message.from_user.id}")
    await message.answer("📝 <b>ЗАГОЛОВОК</b> (до 100 символов):")
    await state.set_state(LongStory.title)
    await state.update_data(user_id=message.from_user.id, username=message.from_user.username or "anon")

async def cancel_long(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Отменено.")

# FSM хендлеры (упрощённые)
async def long_title(message: Message, state: FSMContext):
    if len(message.text) > 100: 
        return await message.answer("❌ До 100 символов!")
    await state.update_data(title=message.text)
    await message.answer("✍️ <b>Часть 1/3</b>")
    await state.set_state(LongStory.part1)

async def long_part1(message: Message, state: FSMContext):
    if len(message.text) > 4000: return await message.answer("❌ До 4000!")
    await state.update_data(part1=message.text)
    await message.answer("✍️ <b>Часть 2/3</b>")
    await state.set_state(LongStory.part2)

async def long_part2(message: Message, state: FSMContext):
    if len(message.text) > 4000: return await message.answer("❌ До 4000!")
    await state.update_data(part2=message.text)
    await message.answer("✍️ <b>Часть 3/3</b>")
    await state.set_state(LongStory.part3)

async def long_part3(message: Message, state: FSMContext):
    if len(message.text) > 4000: return await message.answer("❌ До 4000!")
    await state.update_data(part3=message.text)
    await message.answer("📷 Фото или 'без фото':")
    await state.set_state(LongStory.photo)

async def long_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    full_story = f"<b>{data['title']}</b>\n\n{data['part1']}\n\n{data['part2']}\n\n{data['part3']}"
    
    if message.photo or "без фото" in (message.text or "").lower():
        story = Story(
            user_id=data['user_id'],
            username=data['username'],
            text=full_story,
            type="long_story",
            photo_file_id=message.photo[-1].file_id if message.photo else None
        )
        await state.clear()
        await process_story(Message(  # Заглушка
            text=full_story, 
            from_user=type('User', (), {'id': data['user_id'], 'username': data['username']})(),
            photo=message.photo
        ))
    else:
        await message.answer("❌ Фото или 'без фото'!")


# ---------- МОДЕРАЦИЯ ----------

@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery):
    await call.answer()
    # ... как было ...

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    await call.answer()
    # ... как было ...


# ---------- ЗАПУСК ----------

async def main():
    print("🤖 Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
