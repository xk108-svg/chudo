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

ADMIN_USER_ID = 318289611  # твой Telegram ID
LIMIT_SECONDS = 2 * 24 * 60 * 60  # 2 дня

# user_id -> timestamp последней отправленной истории
last_story_ts: Dict[int, float] = {}

# ✅ БУФЕР ДЛЯ СОБКИ ДЛИННЫХ СООБЩЕНИЙ
message_buffer: Dict[int, List[Dict]] = {}
BUFFER_TIMEOUT = 10  # секунд ожидания новых частей


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
    type: str = "text"  # "text", "photo", "long_story", "buffered"
    photo_file_id: Optional[str] = None


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

async def supabase_request(
    method: str,
    path: str,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
):
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
        async with session.request(
            method, url, headers=headers, json=json, params=params
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = await resp.text()
            if resp.status >= 400:
                print(f"Supabase error {resp.status}: {data}")
                return None
            return data


async def save_story_to_supabase(story: Story) -> Optional[int]:
    if not SUPABASE_ENABLED:
        return None

    payload = {
        "user_id": story.user_id,
        "username": story.username,
        "story": story.text,          
        "status": story.status,
        "type": story.type,
        "photo_file_id": story.photo_file_id,
    }
    data = await supabase_request("POST", "/rest/v1/stories", json=payload)
    if not data:
        return None
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


# ---------- ✅ БУФЕР ДЛЯ ДЛИННЫХ СООБЩЕНИЙ ----------

async def flush_buffer(user_id: int):
    """Собирает все части сообщения пользователя и отправляет в модерацию"""
    if user_id not in message_buffer:
        return
    
    parts = message_buffer.pop(user_id, [])
    if not parts:
        return
    
    print(f"📦 СОБИРАЕМ: {user_id} — {len(parts)} частей")
    
    # Собираем текст и фото
    full_text = ""
    photo_file_id = None
    
    for part in parts:
        if part.get('photo'):
            photo_file_id = part['photo']
        elif part.get('text'):
            full_text += part['text'] + "\n\n"
    
    full_text = full_text.strip()
    username = parts[0].get('username', 'anon')
    
    # Создаем историю
    story = Story(
        user_id=user_id,
        username=username,
        text=full_text or "📷 Только фото",
        type="buffered" if len(parts) > 1 else "text",
        photo_file_id=photo_file_id,
    )
    
    story_id = await save_story_to_supabase(story)
    
    try:
        await bot.send_message(user_id, "✅ История отправлена на модерацию!")
    except:
        pass
    
    # В модерацию
    if MOD_CHAT_ID:
        parts_count = len(parts)
        content_type = "📦 Автосбор" if parts_count > 1 else "📝 Текст"
        if photo_file_id:
            content_type += " + фото"
        
        header = (
            f"🆕 {content_type} ({parts_count} частей)\n"
            f"Автор: @{username} (id {user_id})\n"
            f"📄 {len(full_text)} символов\n"
            f"ID БД: {story_id or 'нет'}\n\n"
        )
        kb = moderation_keyboard(story_id or 0)
        
        try:
            if photo_file_id:
                await bot.send_photo(
                    MOD_CHAT_ID, 
                    photo=photo_file_id, 
                    caption=header + full_text, 
                    reply_markup=kb
                )
            else:
                await bot.send_message(
                    MOD_CHAT_ID, 
                    header + full_text, 
                    reply_markup=kb
                )
            print(f"✅ BUFFER SENT: {user_id} ({parts_count} частей)")
        except Exception as e:
            print(f"❌ BUFFER ERROR: {e}")


# ---------- СЛУЖЕБНЫЕ ФУНКЦИИ ----------

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


def share_your_story_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ Поделись своей историей",
                    url="https://t.me/pishiistorii_bot",
                )
            ]
        ]
    )


def extract_user_id_from_moderation_text(text: str) -> Optional[int]:
    m = re.search(r"\(id (\d+)\)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


# ---------- ТЕКСТЫ ПРИВЕТСТВИЯ ----------

START_MSG_1 = (
    "Добро пожаловать, путник истории.\n"
    "Здесь, как в храме слова, каждый рассказ — маленькое чудо, "
    "которое может согреть чь‑то сердце. "
    "Поделись тем, что пережил, видел или понял — и пусть это послужит другим."
)

START_MSG_2 = (
    "Перед тем как начать, давай позаботимся о чистоте речи:\n"
    "• без политики и споров о власти;\n"
    "• без брани и грубых выражений;\n"
    "• без осуждения, насмешек и оскорблений;\n"
    "• без пропаганды насилия, зависимостей и нечестных поступков.\n\n"
    "Пусть каждое слово будет таким, за которое не стыдно ни перед совестью, "
    "ни перед Богом."
)

START_MSG_3 = (
    "Пиши так, как будто рассказываешь историю перед алтарём:\n"
    "со стремлением к добру, милосердию и свету.\n"
    "Даже если ты описываешь боль или падения, "
    "постарайся завершить рассказ лучом надежды — "
    "уроком, выводом, шагом к очищению сердца."
)

START_MSG_4 = (
    "В конце истории ты можешь добавить до двух хештегов для поиска.\n"
    "Например:\n"
    "#семья #чудо\n"
    "или\n"
    "#исцеление #путькБогу\n\n"
    "Хештеги ставь в самом низу сообщения, слитно, без пробелов внутри."
)


# ---------- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ----------

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(START_MSG_1)
    await message.answer(START_MSG_2)
    await message.answer(START_MSG_3)
    await message.answer(START_MSG_4)


@router.message(F.text.startswith("/ad "))
async def cmd_ad(message: Message):
    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1]
        ad_text = message.text[4:].strip()
        
        if not ad_text:
            await message.answer("❌ После /ad напиши текст объявления.")
            return
            
        await bot.send_photo(
            CHANNEL_ID,
            photo=photo.file_id,
            caption=f"📢 <b>Реклама</b>\n\n{ad_text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✍️ Поделись своей историей",
                    url="https://t.me/pishiistorii_bot"
                )]
            ]),
        )
        
        try:
            await message.reply_to_message.delete()
            await message.delete()
        except:
            pass
            
        confirm = await message.answer("✅ Реклама опубликована!")
        await asyncio.sleep(3)
        try:
            await confirm.delete()
        except:
            pass
        return

    ad_text = message.text[4:].strip()
    if not ad_text:
        await message.answer("❌ После /ad напиши текст объявления.")
        return

    await bot.send_message(
        CHANNEL_ID,
        f"📢 <b>Реклама</b>\n\n{ad_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✍️ Поделись своей историей",
                url="https://t.me/pishiistorii_bot"
            )]
        ]),
    )
    
    try:
        await message.delete()
    except:
        pass
    
    confirm = await message.answer("✅ Реклама опубликована!")
    await asyncio.sleep(3)
    try:
        await confirm.delete()
    except:
        pass


# ---------- ДЛИННАЯ ИСТОРИЯ (/long_story) ----------

@router.message(F.text == "/long_story")
async def cmd_long_story(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await message.answer("⏳ Ты уже пишешь историю! Заверши её или /cancel")
        return
        
    print(f"🚀 START LONG STORY: {message.from_user.id}")
    await message.answer(
        "📝 <b>Длинная история (до 30 000 символов)</b>\n\n"
        "Напиши <b>ЗАГОЛОВОК</b> (до 100 символов):"
    )
    await state.set_state(LongStory.title)
    await state.update_data(
        user_id=message.from_user.id,
        username=message.from_user.username or "anon"
    )


@router.message(F.text == "/cancel")
async def cancel_long(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активной длинной истории.")
        return
        
    await state.clear()
    await message.answer("✅ Длинная история отменена.")


@router.message(LongStory.title)
async def long_title(message: Message, state: FSMContext):
    print(f"TITLE: {message.from_user.id} - {len(message.text)} символов")
    if len(message.text) > 100:
        await message.answer("❌ Заголовок до 100 символов! Попробуй ещё раз:")
        return
    await state.update_data(title=message.text)
    await message.answer("✍️ <b>Часть 1/3</b> (до 4000 символов):")
    await state.set_state(LongStory.part1)


@router.message(LongStory.part1)
async def long_part1(message: Message, state: FSMContext):
    print(f"PART1: {message.from_user.id} - {len(message.text)}
