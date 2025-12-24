import asyncio
import os
import time
import re
from dataclasses import dataclass
from typing import Optional, Dict, List
from collections import defaultdict

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

# 🔥 БУФЕР ДЛЯ СКЛЕЙКИ ЧАСТЕЙ (story_id -> список частей)
pending_stories: Dict[int, List[Dict]] = {}

# ---------- НАСТРОЙКИ ----------

ADMIN_USER_ID = 318289611
LIMIT_SECONDS = 2 * 24 * 60 * 60  # 2 дня
last_story_ts: Dict[int, float] = {}

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

print("🤖 ENV CHECK:")
print(f"BOT_TOKEN: {'✅' if BOT_TOKEN else '❌'}")
print(f"CHANNEL_ID: {CHANNEL_ID_RAW}")
print(f"MOD_CHAT_ID: {MOD_CHAT_ID_RAW}")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан!")
if not CHANNEL_ID_RAW:
    raise ValueError("❌ CHANNEL_ID не задан!")

CHANNEL_ID = int(CHANNEL_ID_RAW)
MOD_CHAT_ID = int(MOD_CHAT_ID_RAW) if MOD_CHAT_ID_RAW else None
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

# ---------- БОТ ----------

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ---------- SUPABASE ----------

async def supabase_request(method: str, path: str, json=None, params=None):
    if not SUPABASE_ENABLED: return None
    url = f"{SUPABASE_URL}{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=json, params=params) as resp:
            try:
                data = await resp.json(content_type=None)
            except:
                data = await resp.text()
            if resp.status >= 400:
                print(f"❌ Supabase {resp.status}: {data}")
                return None
            return data

async def save_story_to_supabase(story: Story) -> Optional[int]:
    if not SUPABASE_ENABLED: return None
    payload = {
        "user_id": story.user_id, "username": story.username,
        "story": story.text, "status": story.status,
        "type": story.type, "photo_file_id": story.photo_file_id
    }
    data = await supabase_request("POST", "/rest/v1/stories", json=payload)
    return data[0]["id"] if data else None

async def delete_story_from_supabase(story_id: int) -> bool:
    if not SUPABASE_ENABLED: return False
    data = await supabase_request("DELETE", "/rest/v1/stories", params={"id": f"eq.{story_id}"})
    return bool(data)

# ---------- КНОПКИ ----------

def moderation_keyboard(story_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{story_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{story_id}")
        ]
    ])

def share_your_story_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✍️ Поделись своей историей", url="https://t.me/pishiistorii_bot")]
    ])

def extract_user_id_from_moderation_text(text: str) -> Optional[int]:
    m = re.search(r"\(id (\d+)\)", text)
    return int(m.group(1)) if m else None

# ---------- СТАРТ ----------

START_MSGS = [
    "Добро пожаловать, путник истории. Здесь каждый рассказ — чудо.",
    "Чистота речи: без политики, брани, осуждения.",
    "Пиши с добром и милосердием.",
    "Хештеги: #семья #чудо"
]

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    for msg in START_MSGS:
        await message.answer(msg)

# ---------- РЕКЛАМА ----------

@router.message(F.text.startswith("/ad "))
async def cmd_ad(message: Message):
    ad_text = message.text[4:].strip()
    if not ad_text:
        return await message.answer("❌ После /ad текст!")
    
    kb = share_your_story_keyboard()
    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1]
        await bot.send_photo(CHANNEL_ID, photo.file_id, caption=f"📢 <b>Реклама</b>\n\n{ad_text}", reply_markup=kb)
        try:
            await message.reply_to_message.delete()
            await message.delete()
        except: pass
    else:
        await bot.send_message(CHANNEL_ID, f"📢 <b>Реклама</b>\n\n{ad_text}", reply_markup=kb)
        try: await message.delete()
        except: pass
    
    confirm = await message.answer("✅ Реклама опубликована!")
    await asyncio.sleep(3)
    try: await confirm.delete()
    except: pass

# 🔥 ОСНОВНОЙ ХЕНДЛЕР + БУФЕР
@router.message(
    (F.photo & ~F.reply_to_message) | 
    (F.text & ~F.text.startswith(("/ad", "/start")))
)
async def handle_story(message: Message):
    print(f"📨 {message.from_user.id}: {len(message.text or message.caption or '')} симв.")
    
    user = message.from_user
    if user.id != ADMIN_USER_ID:
        now = time.time()
        last_ts = last_story_ts.get(user.id)
        if last_ts and now - last_ts < LIMIT_SECONDS:
            hours_left = int((LIMIT_SECONDS - (now - last_ts)) // 3600) + 1
            return await message.answer(f"⏳ Подожди {hours_left} ч")
        last_story_ts[user.id] = now

    text = message.caption or message.text or ""
    has_photo = message.photo is not None
    photo_file_id = message.photo[-1].file_id if has_photo else None
    story_type = "photo" if has_photo else "text"

    story = Story(None, user.id, user.username or "anon", text, type=story_type, photo_file_id=photo_file_id)
    story_id = await save_story_to_supabase(story)
    
    # 🔥 СОХРАНЯЕМ ЧАСТЬ
    if story_id and story_id not in pending_stories:
        pending_stories[story_id] = []
    if story_id:
        pending_stories[story_id].append({'text': text, 'photo': photo_file_id})

    await message.answer("✅ На модерацию!")

    if MOD_CHAT_ID:
        content_type = "📷 Фото" if has_photo else "📝 Текст"
        header = f"🆕 {content_type}\n@{story.username} (id {story.user_id})\nID: {story_id or 'нет'}\n\n"
        kb = moderation_keyboard(story_id or 0)
        
        try:
            if has_photo:
                await bot.send_photo(MOD_CHAT_ID, photo_file_id, caption=header + text, reply_markup=kb)
            else:
                await bot.send_message(MOD_CHAT_ID, header + text, reply_markup=kb)
            print(f"✅ В МОД: {story_id}")
        except Exception as e:
            print(f"❌ МОД ОШИБКА: {e}")

# 🔥 СКЛЕЙКА ПРИ ОДОБРЕНИИ
@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery):
    await call.answer()
    story_id = int(call.data.split(":", 1)[1])
    
    # 🔥 СКЛЕИВАЕМ!
    full_text = ""
    photo_file_id = None
    if story_id in pending_stories:
        parts = pending_stories[story_id]
        print(f"🔗 СКЛЕЙКА: {len(parts)} частей")
        for part in parts:
            if part['photo']: 
                photo_file_id = part['photo']
            else: 
                full_text += part['text'] + "\n\n"
        full_text = full_text.strip()
        del pending_stories[story_id]
    else:
        full_text = (call.message.caption or call.message.text or "").split("\n", 4)[-1].strip()
        photo_file_id = call.message.photo[-1].file_id if call.message.photo else None
    
    # ПУБЛИКАЦИЯ
    kb = share_your_story_keyboard()
    try:
        if photo_file_id:
            await bot.send_photo(CHANNEL_ID, photo_file_id, caption=full_text or None, reply_markup=kb)
        else:
            await bot.send_message(CHANNEL_ID, full_text or " ", reply_markup=kb)
        print("✅ В КАНАЛ!")
    except Exception as e:
        print(f"❌ КАНАЛ: {e}")
        return
    
    if story_id: await delete_story_from_supabase(story_id)
    
    user_id = extract_user_id_from_moderation_text(call.message.caption or call.message.text or "")
    if user_id:
        try: await bot.send_message(user_id, "✨ Опубликовано!")
        except: pass
    
    new_text = (call.message.caption or call.message.text or "") + "\n\n✅ Одобрено!"
    try:
        if call.message.photo: await call.message.edit_caption(new_text)
        else: await call.message.edit_text(new_text)
    except: pass

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    await call.answer()
    story_id = int(call.data.split(":", 1)[1]) if len(call.data.split(":")) > 1 else 0
    
    if story_id:
        await delete_story_from_supabase(story_id)
        if story_id in pending_stories: del pending_stories[story_id]
    
    user_id = extract_user_id_from_moderation_text(call.message.caption or call.message.text or "")
    if user_id:
        try: await bot.send_message(user_id, "❌ Отклонено")
        except: pass
    
    new_text = (call.message.caption or call.message.text or "") + "\n\n❌ Отклонено"
    try:
        if call.message.photo: await call.message.edit_caption(new_text)
        else: await call.message.edit_text(new_text)
    except: pass

# ---------- ЗАПУСК ----------

async def main():
    print("🤖 Запуск...")
    print(f"📺 {CHANNEL_ID} | 🛡️ {MOD_CHAT_ID or 'нет'}")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
