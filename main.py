import asyncio
import os
import time
import re
from dataclasses import dataclass
from typing import Optional, Dict, List
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
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
LIMIT_SECONDS = 2 * 24 * 60 * 60  # 2 дня

last_story_ts: Dict[int, float] = {}
message_buffer: Dict[int, List[Dict]] = {}
BUFFER_TIMEOUT = 10  # секунд


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


# ---------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ----------

BOT_TOKEN = os.getenv("BOT_TOKEN")
MOD_CHAT_ID_RAW = os.getenv("MOD_CHAT_ID")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print("🤖 ENV CHECK:")
print(f"  BOT_TOKEN: {'✅' if BOT_TOKEN else '❌'}")
print(f"  CHANNEL_ID: {CHANNEL_ID_RAW}")
print(f"  MOD_CHAT_ID: {MOD_CHAT_ID_RAW}")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан!")
if not CHANNEL_ID_RAW:
    raise ValueError("❌ CHANNEL_ID не задан!")

CHANNEL_ID = int(CHANNEL_ID_RAW)
MOD_CHAT_ID = int(MOD_CHAT_ID_RAW) if MOD_CHAT_ID_RAW else None
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)


# ---------- ИНИЦИАЛИЗАЦИЯ ----------

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ---------- SUPABASE ----------

async def supabase_request(method: str, path: str, json=None, params=None):
    if not SUPABASE_ENABLED: 
        print("⚠️ Supabase отключен")
        return None
    
    url = f"{SUPABASE_URL}{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
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
        "user_id": story.user_id, 
        "username": story.username,
        "story": story.text, 
        "status": story.status,
        "type": story.type, 
        "photo_file_id": story.photo_file_id,
    }
    
    data = await supabase_request("POST", "/rest/v1/stories", json=payload)
    if data and len(data) > 0:
        return data[0].get("id")
    return None

async def delete_story_from_supabase(story_id: int) -> bool:
    if not SUPABASE_ENABLED: return False
    params = {"id": f"eq.{story_id}"}
    data = await supabase_request("DELETE", "/rest/v1/stories", params=params)
    return bool(data)


# ---------- БУФЕР ДЛЯ ДЛИННЫХ СООБЩЕНИЙ ----------

async def flush_buffer(user_id: int):
    if user_id not in message_buffer: 
        return
        
    parts = message_buffer.pop(user_id, [])
    if not parts: 
        return
    
    print(f"📦 СОБИРАЕМ: {user_id} — {len(parts)} частей")
    
    full_text = ""
    photo_file_id = None
    username = parts[0].get('username', 'anon')
    
    for part in parts:
        if part.get('photo'):
            photo_file_id = part['photo']
        elif part.get('text'):
            full_text += part['text'] + "\n\n"
    
    full_text = full_text.strip()
    if not full_text and not photo_file_id:
        return
        
    story = Story(
        user_id=user_id, 
        username=username,
        text=full_text or "📷 Только фото",
        type="buffered" if len(parts) > 1 else "text",
        photo_file_id=photo_file_id
    )
    
    story_id = await save_story_to_supabase(story)
    
    # Подтверждение пользователю
    try:
        await bot.send_message(user_id, "✅ История отправлена на модерацию!")
    except Exception as e:
        print(f"❌ Не удалось уведомить {user_id}: {e}")
    
    # В модерацию
    if MOD_CHAT_ID:
        parts_count = len(parts)
        content_type = f"📦 Автосбор ({parts_count} частей)"
        if photo_file_id: 
            content_type += " + фото"
        
        header = (
            f"🆕 {content_type}\n"
            f"Автор: @{username} (id {user_id})\n"
            f"📄 {len(full_text)} символов\n"
            f"ID БД: {story_id or 'нет'}\n\n"
        )
        kb = moderation_keyboard(story_id or 0)
        
        try:
            if photo_file_id:
                await bot.send_photo(
                    MOD_CHAT_ID, 
                    photo_file_id, 
                    caption=header + full_text, 
                    reply_markup=kb
                )
            else:
                await bot.send_message(
                    MOD_CHAT_ID, 
                    header + full_text, 
                    reply_markup=kb
                )
            print(f"✅ BUFFER → МОДЕРАЦИЯ: {user_id}")
        except Exception as e:
            print(f"❌ BUFFER ERROR: {e}")


# ---------- КНОПКИ ----------

def moderation_keyboard(story_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{story_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{story_id}")
        ]
    ])

def share_your_story_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Поделись своей историей", url="https://t.me/pishiistorii_bot")]
    ])


def extract_user_id_from_moderation_text(text: str) -> Optional[int]:
    m = re.search(r"\(id (\d+)\)", text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


# ---------- СТАРТОВЫЕ СООБЩЕНИЯ ----------

START_MSG_1 = (
    "Добро пожаловать, путник истории.\n"
    "Здесь, как в храме слова, каждый рассказ — маленькое чудо, "
    "которое может согреть чь‑то сердце."
)

START_MSG_2 = (
    "Перед тем как начать, давай позаботимся о чистоте речи:\n"
    "• без политики и споров о власти;\n"
    "• без брани и грубых выражений;\n"
    "• без осуждения, насмешек и оскорблений;\n"
    "• без пропаганды насилия, зависимостей и нечестных поступков."
)

START_MSG_3 = (
    "Пиши так, как будто рассказываешь историю перед алтарём:\n"
    "со стремлением к добру, милосердию и свету."
)

START_MSG_4 = (
    "В конце истории ты можешь добавить до двух хештегов:\n"
    "#семья #чудо\n"
    "#исцеление #путькБогу"
)


# ---------- ХЕНДЛЕРЫ ----------

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(START_MSG_1)
    await message.answer(START_MSG_2)
    await message.answer(START_MSG_3)
    await message.answer(START_MSG_4, reply_markup=share_your_story_keyboard())


@router.message(F.text.startswith("/ad "))
async def cmd_ad(message: Message):
    ad_text = message.text[4:].strip()
    if not ad_text:
        return await message.answer("❌ После /ad напиши текст объявления.")
    
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
        try:
            await message.delete()
        except: pass
    
    confirm = await message.answer("✅ Реклама опубликована!")
    await asyncio.sleep(3)
    try:
        await confirm.delete()
    except: pass


@router.message(F.text == "/long_story")
async def cmd_long_story(message: Message, state: FSMContext):
    if await state.get_state():
        return await message.answer("⏳ Ты уже пишешь историю! Заверши её или /cancel")
    
    print(f"🚀 LONG STORY: {message.from_user.id}")
    await message.answer("📝 <b>ЗАГОЛОВОК</b> (до 100 символов):")
    await state.set_state(LongStory.title)
    await state.update_data(
        user_id=message.from_user.id, 
        username=message.from_user.username or "anon"
    )


@router.message(F.text == "/cancel")
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Отменено")


# 🔥 ФИКСИРОВАННЫЙ FSM ДЛЯ ДЛИННЫХ ИСТОРИЙ
@router.message(LongStory.title)
async def long_title(message: Message, state: FSMContext):
    if len(message.text) > 100:
        return await message.answer("❌ Заголовок до 100 символов!")
    await state.update_data(title=message.text)
    await message.answer("✍️ <b>Часть 1/3</b> (до 4000 символов):")
    await state.set_state(LongStory.part1)

@router.message(LongStory.part1)
async def long_part1(message: Message, state: FSMContext):
    await state.update_data(part1=message.text)
    await message.answer("✍️ <b>Часть 2/3</b>")
    await state.set_state(LongStory.part2)

@router.message(LongStory.part2)
async def long_part2(message: Message, state: FSMContext):
    await state.update_data(part2=message.text)
    await message.answer("✍️ <b>Часть 3/3</b>")
    await state.set_state(LongStory.part3)

@router.message(LongStory.part3)
async def long_part3(message: Message, state: FSMContext):
    await state.update_data(part3=message.text)
    await message.answer("📷 Отправь фото или напиши 'без фото':")
    await state.set_state(LongStory.photo)

@router.message(LongStory.photo)
async def long_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    full_story = f"<b>{data['title']}</b>\n\n{data['part1']}\n\n{data['part2']}\n\n{data['part3']}"
    
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    elif "без фото" not in (message.text or "").lower():
        return await message.answer("❌ Отправь фото или напиши 'без фото'")
    
    story = Story(
        user_id=data['user_id'], 
        username=data['username'], 
        text=full_story, 
        type="long_story",
        photo_file_id=photo_file_id
    )
    
    story_id = await save_story_to_supabase(story)
    await state.clear()
    
    await message.answer("✅ Длинная история отправлена на модерацию!")
    
    if MOD_CHAT_ID:
        header = f"🆕 <b>ДЛИННАЯ ИСТОРИЯ</b>\n@{data['username']} (id {data['user_id']})\n\n"
        kb = moderation_keyboard(story_id or 0)
        
        if photo_file_id:
            await bot.send_photo(MOD_CHAT_ID, photo_file_id, caption=header + full_story, reply_markup=kb)
        else:
            await bot.send_message(MOD_CHAT_ID, header + full_story, reply_markup=kb)


# 🔥 ✅ ОСНОВНОЙ ХЕНДЛЕР — ЛОВИТ ВСЁ!
@router.message(
    F.content_type.in_({ContentType.TEXT, ContentType.PHOTO}) & 
    ~F.reply_to_message & 
    ~F.text.startswith(("/ad ", "/start", "/long_story", "/cancel"))
)
async def handle_story_buffered(message: Message, state: FSMContext):
    """🎯 ЛОВИТ ТЕКСТЫ И ФОТО — РАБОТАЕТ 100%!"""
    
    if await state.get_state():
        print(f"⏳ FSM активен для {message.from_user.id}")
        return
    
    user_id = message.from_user.id
    now = time.time()
    text_content = message.text or message.caption or ""
    
    print(f"📨 СООБЩЕНИЕ: {user_id} | {len(text_content)} символов | photo={bool(message.photo)}")
    
    # Лимит для не-админов
    if user_id != ADMIN_USER_ID:
        last_ts = last_story_ts.get(user_id)
        if last_ts and now - last_ts < LIMIT_SECONDS:
            hours_left = int((LIMIT_SECONDS - (now - last_ts)) // 3600) + 1
            await message.answer(f"⏳ Подожди {hours_left} ч")
            return
        last_story_ts[user_id] = now
    
    # ✅ БУФЕР
    message_buffer.setdefault(user_id, []).append({
        'timestamp': now,
        'text': text_content,
        'photo': message.photo[-1].file_id if message.photo else None,
        'username': message.from_user.username or "anon"
    })
    
    # ✅ КОРОТКИЕ ТЕКСТЫ — СРАЗУ В МОДЕРАЦИЮ!
    if len(text_content) < 500 and not message.photo:
        print(f"⚡ КОРОТКОЕ СРАЗУ: {user_id} ({len(text_content)} символов)")
        await flush_buffer(user_id)
        return
    
    # Длинные — ждём 10 секунд
    print(f"⏳ БУФЕР: {user_id} — ждём {BUFFER_TIMEOUT}с")
    
    async def timeout_flush():
        await asyncio.sleep(BUFFER_TIMEOUT)
        if user_id in message_buffer:  # ещё не очистили
            await flush_buffer(user_id)
    
    asyncio.create_task(timeout_flush())


# ---------- МОДЕРАЦИЯ ----------

@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery):
    await call.answer()
    story_id = int(call.data.split(":", 1)[1])
    
    full_text = call.message.caption or call.message.text or ""
    # Извлекаем чистый текст истории (после заголовка)
    lines = full_text.split("\n")
    story_text = "\n".join(lines[4:]).strip() if len(lines) > 4 else full_text
    
    kb = share_your_story_keyboard()
    
    try:
        if call.message.photo:
            await bot.send_photo(
                CHANNEL_ID, 
                call.message.photo[-1].file_id, 
                caption=story_text or None, 
                reply_markup=kb
            )
        else:
            await bot.send_message(CHANNEL_ID, story_text or " ", reply_markup=kb)
        
        # Уведомляем автора
        user_id = extract_user_id_from_moderation_text(full_text)
        if user_id:
            await bot.send_message(user_id, "✨ Твоя история опубликована в канале!")
        
        # Удаляем из БД
        if story_id:
            await delete_story_from_supabase(story_id)
        
        # Обновляем сообщение модерации
        new_text = full_text + "\n\n✅ <b>Одобрено и опубликовано!</b>"
        if call.message.photo:
            await call.message.edit_caption(new_text)
        else:
            await call.message.edit_text(new_text)
            
        print(f"✅ ОДОБРЕНО: {story_id}")
        
    except Exception as e:
        print(f"❌ APPROVE ERROR: {e}")
        await call.message.answer(f"❌ Ошибка публикации: {e}")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    await call.answer()
    story_id = int(call.data.split(":", 1)[1]) if ":" in call.data else 0
    
    full_text = call.message.caption or call.message.text or ""
    user_id = extract_user_id_from_moderation_text(full_text)
    
    # Удаляем из БД
    if story_id:
        await delete_story_from_supabase(story_id)
    
    # Уведомляем автора
    if user_id:
        try:
            await bot.send_message(user_id, "❌ История не прошла модерацию")
        except: pass
    
    # Обновляем сообщение
    new_text = full_text + "\n\n❌ <b>Отклонено</b>"
    if call.message.photo:
        await call.message.edit_caption(new_text)
    else:
        await call.message.edit_text(new_text)
    
    print(f"❌ ОТКЛОНЕНО: {story_id}")


# ---------- ЗАПУСК ----------

async def main():
    print("🤖 Запуск бота...")
    print(f"📺 Канал: {CHANNEL_ID}")
    print(f"🛡️ Модерация: {MOD_CHAT_ID or 'отключена'}")
    print("🚀 Готов к работе!")
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        print("🛑 Остановка бота...")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
