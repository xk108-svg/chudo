import asyncio
import os
import time
import re
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from collections import defaultdict
from datetime import datetime, timedelta

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


# 🔥 ГЛОБАЛЬНЫЙ БУФЕР ДЛЯ ЧАСТЕЙ ИСТОРИЙ
user_stories: Dict[int, List[Dict]] = {}
USER_BUFFER_SIZE = 10  # увеличиваем для длинных историй

# 🔥 СЛОВАРЬ ДЛЯ ОТСЛЕЖИВАНИЯ ИСТОРИЙ В МОДЕРАЦИИ
# user_id -> {story_index: message_id_in_moderation}
moderation_messages: Dict[int, Dict[int, int]] = defaultdict(dict)
# user_id -> список ID сообщений в канале (для публикации)
pending_publication: Dict[int, List[Dict]] = defaultdict(list)


# ---------- НАСТРОЙКИ ПРОЕКТА ----------

ADMIN_USER_ID = 318289611  # твой Telegram ID
LIMIT_SECONDS = 2 * 24 * 60 * 60  # 2 дня

# user_id -> timestamp последней отправленной истории
last_story_ts: Dict[int, float] = {}


# ---------- МОДЕЛЬ ИСТОРИИ ----------

@dataclass
class StoryPart:
    index: int  # номер части (1, 2, 3...)
    user_id: int
    username: str
    text: str
    status: str = "pending"
    type: str = "text"  # "text" или "photo"
    photo_file_id: Optional[str] = None
    message_id: Optional[int] = None  # ID сообщения в модерации
    timestamp: float = None


# ---------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ----------

BOT_TOKEN = os.getenv("BOT_TOKEN")
MOD_CHAT_ID_RAW = os.getenv("MOD_CHAT_ID")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print("ENV BOT_TOKEN:", bool(BOT_TOKEN))
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


async def save_story_part_to_supabase(part: StoryPart) -> Optional[int]:
    """Сохраняет часть истории, возвращает ID записи или None."""
    if not SUPABASE_ENABLED:
        return None

    payload = {
        "user_id": part.user_id,
        "username": part.username,
        "story": part.text,          
        "status": part.status,
        "type": part.type,
        "photo_file_id": part.photo_file_id,
        "part_index": part.index,
        "timestamp": part.timestamp or time.time(),
    }
    data = await supabase_request("POST", "/rest/v1/story_parts", json=payload)
    if not data:
        return None
    try:
        return data[0]["id"]
    except Exception as e:
        print("Parse Supabase insert response error:", e, data)
        return None


# ---------- СЛУЖЕБНЫЕ ФУНКЦИИ ----------

def moderation_keyboard(user_id: int, part_index: int = None) -> InlineKeyboardMarkup:
    """Кнопки модерации для конкретной части"""
    if part_index is not None:
        callback_data = f"approve_part:{user_id}:{part_index}"
        reject_data = f"reject_part:{user_id}:{part_index}"
    else:
        callback_data = f"approve:{user_id}"
        reject_data = f"reject:{user_id}"
    
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить эту часть",
                    callback_data=callback_data,
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить эту часть",
                    callback_data=reject_data,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Показать все части",
                    callback_data=f"show_parts:{user_id}",
                ),
                InlineKeyboardButton(
                    text="🚀 Опубликовать всё",
                    callback_data=f"publish_all:{user_id}",
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
    """
    Ищет в тексте строку вида '(id 123456789)' и возвращает число.
    """
    m = re.search(r"\(id (\d+)\)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


async def send_part_to_moderation(part: StoryPart, is_first: bool = False):
    """Отправляет часть истории в чат модерации"""
    if not MOD_CHAT_ID:
        return None
    
    user_id = part.user_id
    index = part.index
    
    header = (
        f"📝 Часть {index} из {len(user_stories.get(user_id, []))}\n"
        f"Автор: @{part.username} (id {user_id})\n"
        f"Время: {datetime.fromtimestamp(part.timestamp).strftime('%H:%M:%S')}\n"
    )
    
    if is_first:
        header += "🆕 НАЧАЛО НОВОЙ ИСТОРИИ\n\n"
    
    full_text = header + part.text
    
    try:
        if part.photo_file_id:
            msg = await bot.send_photo(
                MOD_CHAT_ID,
                photo=part.photo_file_id,
                caption=full_text,
                reply_markup=moderation_keyboard(user_id, part.index),
            )
        else:
            msg = await bot.send_message(
                MOD_CHAT_ID,
                full_text,
                reply_markup=moderation_keyboard(user_id, part.index),
            )
        
        # Сохраняем ID сообщения в модерации
        moderation_messages[user_id][index] = msg.message_id
        part.message_id = msg.message_id
        
        print(f"✅ Отправлена в модерацию часть {index} от user_id={user_id}")
        return msg.message_id
        
    except Exception as e:
        print(f"❌ Ошибка отправки в модерацию: {e}")
        return None


async def publish_all_parts(user_id: int):
    """Публикует все одобренные части в канал"""
    if user_id not in user_stories:
        print(f"❌ Нет частей для публикации user_id={user_id}")
        return False
    
    parts = user_stories[user_id]
    if not parts:
        return False
    
    # Сортируем части по индексу
    sorted_parts = sorted(parts, key=lambda x: x.get('index', 0))
    
    published_count = 0
    for part in sorted_parts:
        try:
            text = part['text']
            photo_file_id = part.get('photo')
            
            # Добавляем номер части в начало
            part_header = f"Часть {part['index']}\n\n" if len(sorted_parts) > 1 else ""
            full_text = part_header + text
            
            kb = share_your_story_keyboard() if part['index'] == len(sorted_parts) else None
            
            if photo_file_id:
                await bot.send_photo(
                    CHANNEL_ID,
                    photo=photo_file_id,
                    caption=full_text if text else None,
                    reply_markup=kb,
                )
            else:
                await bot.send_message(
                    CHANNEL_ID,
                    full_text,
                    reply_markup=kb,
                )
            
            published_count += 1
            print(f"✅ Опубликована часть {part['index']} от user_id={user_id}")
            
            # Небольшая задержка между отправками
            if published_count < len(sorted_parts):
                await asyncio.sleep(0.5)
                
        except Exception as e:
            print(f"❌ Ошибка публикации части {part.get('index')}: {e}")
    
    # Очищаем буфер после публикации
    if user_id in user_stories:
        del user_stories[user_id]
    if user_id in moderation_messages:
        del moderation_messages[user_id]
    
    # Уведомляем автора
    try:
        await bot.send_message(
            chat_id=user_id,
            text=f"✨ Твоя история ({published_count} частей) опубликована в канале! Спасибо, что делишься!",
        )
        print(f"✅ Уведомлён автор {user_id}")
    except Exception as e:
        print(f"❌ Не удалось уведомить автора: {e}")
    
    return True


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
    "💡 <b>Если история длинная</b>, Telegram может разбить её на несколько сообщений.\n"
    "Это нормально! Присылай все части подряд.\n\n"
    "В конце <b>последней части</b> можешь добавить до двух хештегов для поиска.\n"
    "Например:\n"
    "#семья #чудо\n"
    "или\n"
    "#исцеление #путькБогу\n\n"
    "Хештеги ставь в самом низу сообщения, слитно, без пробелов внутри."
)


# ---------- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЬЯ ----------

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(START_MSG_1)
    await message.answer(START_MSG_2)
    await message.answer(START_MSG_3)
    await message.answer(START_MSG_4, parse_mode=ParseMode.HTML)


@router.message(F.text.startswith("/ad "))
async def cmd_ad(message: Message):
    """
    ✅ РЕКЛАМА БЕЗ ЛИШНЕЙ КНОПКИ + КАРТИНКА ОДНИМ ПОСТОМ
    """
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


# 🔥 ОСНОВНОЙ ХЕНДЛЕР - СБОР ЧАСТЕЙ ИСТОРИИ
@router.message(
    (F.photo & ~F.reply_to_message) | 
    (F.text & ~F.text.startswith(("/ad", "/start")))
)
async def handle_story(message: Message):
    """
    ✅ Собирает части истории от пользователя
    ✅ Каждая часть сразу идет в модерацию
    ✅ При одобрении последней части - всё публикуется
    """
    print(f"📨 Получено сообщение от {message.from_user.id}")
    
    user = message.from_user
    user_id = user.id

    # Ограничение по времени для обычных пользователей
    if user.id != ADMIN_USER_ID:
        now = time.time()
        last_ts = last_story_ts.get(user_id)
        
        # Проверяем, начинается ли новая история
        # (если прошло больше 5 минут с последней части - считаем новой историей)
        is_new_story = False
        if user_id in user_stories and user_stories[user_id]:
            last_part_time = user_stories[user_id][-1].get('timestamp', 0)
            if now - last_part_time > 300:  # 5 минут
                is_new_story = True
                # Очищаем старые части
                del user_stories[user_id]
                if user_id in moderation_messages:
                    del moderation_messages[user_id]
        
        if last_ts and now - last_ts < LIMIT_SECONDS and is_new_story:
            hours_left = int((LIMIT_SECONDS - (now - last_ts)) // 3600) + 1
            await message.answer(
                f"⏳ Ты уже делился историей недавно.\n"
                f"Пожалуйста, приходи с новой историей через примерно {hours_left} ч."
            )
            return
        
        if is_new_story:
            last_story_ts[user_id] = now

    # Получаем данные из сообщения
    text = message.caption or message.text or ""
    has_photo = message.photo is not None
    photo_file_id = message.photo[-1].file_id if has_photo else None
    
    # Инициализируем буфер для пользователя
    if user_id not in user_stories:
        user_stories[user_id] = []
    
    # Определяем индекс части
    part_index = len(user_stories[user_id]) + 1
    
    # Сохраняем часть в буфер
    story_part = {
        'index': part_index,
        'text': text,
        'photo': photo_file_id,
        'username': user.username or "anon",
        'timestamp': time.time(),
        'status': 'pending',
        'type': 'photo' if has_photo else 'text'
    }
    user_stories[user_id].append(story_part)
    
    # Отправляем в модерацию
    moderation_msg_id = await send_part_to_moderation(
        StoryPart(**story_part),
        is_first=(part_index == 1)
    )
    
    # Сохраняем в Supabase
    if SUPABASE_ENABLED:
        await save_story_part_to_supabase(StoryPart(**story_part))
    
    # Уведомляем пользователя
    if part_index == 1:
        await message.answer(
            f"📝 Принята 1-я часть истории.\n"
            f"Присылай следующую часть, если история длинная.\n"
            f"Все части будут опубликованы вместе после модерации."
        )
    else:
        await message.answer(
            f"✅ Часть {part_index} принята.\n"
            f"Всего частей: {part_index}"
        )
    
    print(f"📚 User {user_id}: сохранена часть {part_index}, всего {len(user_stories[user_id])} частей")


# 🔥 ОДОБРЕНИЕ ОТДЕЛЬНОЙ ЧАСТИ
@router.callback_query(F.data.startswith("approve_part:"))
async def cb_approve_part(call: CallbackQuery):
    await call.answer("✅ Часть одобрена!")
    
    try:
        _, user_id_str, part_index_str = call.data.split(":")
        user_id = int(user_id_str)
        part_index = int(part_index_str)
    except:
        await call.message.answer("❌ Ошибка в данных")
        return
    
    print(f"✅ Одобрена часть {part_index} от user_id={user_id}")
    
    # Обновляем статус в буфере
    if user_id in user_stories:
        for part in user_stories[user_id]:
            if part.get('index') == part_index:
                part['status'] = 'approved'
                break
    
    # Помечаем в модерации
    current_text = call.message.caption or call.message.text or ""
    new_text = current_text + "\n\n✅ <b>Часть одобрена</b>"
    
    try:
        if call.message.photo:
            await call.message.edit_caption(new_text)
        else:
            await call.message.edit_text(new_text)
    except Exception as e:
        print(f"Ошибка редактирования: {e}")
    
    # Проверяем, все ли части одобрены
    if user_id in user_stories:
        all_approved = all(part.get('status') == 'approved' for part in user_stories[user_id])
        if all_approved:
            await call.message.answer(
                f"🎉 Все части истории от user_id={user_id} одобрены!\n"
                f"Нажмите '🚀 Опубликовать всё' для публикации."
            )


# 🔥 ОТКЛОНЕНИЕ ЧАСТИ
@router.callback_query(F.data.startswith("reject_part:"))
async def cb_reject_part(call: CallbackQuery):
    await call.answer("❌ Часть отклонена")
    
    try:
        _, user_id_str, part_index_str = call.data.split(":")
        user_id = int(user_id_str)
        part_index = int(part_index_str)
    except:
        await call.message.answer("❌ Ошибка в данных")
        return
    
    print(f"❌ Отклонена часть {part_index} от user_id={user_id}")
    
    # Удаляем часть из буфера
    if user_id in user_stories:
        user_stories[user_id] = [p for p in user_stories[user_id] if p.get('index') != part_index]
        # Перенумеровываем оставшиеся части
        for i, part in enumerate(user_stories[user_id], 1):
            part['index'] = i
    
    # Помечаем в модерации
    current_text = call.message.caption or call.message.text or ""
    new_text = current_text + "\n\n❌ <b>Часть отклонена</b>"
    
    try:
        if call.message.photo:
            await call.message.edit_caption(new_text)
        else:
            await call.message.edit_text(new_text)
    except Exception as e:
        print(f"Ошибка редактирования: {e}")
    
    # Уведомляем пользователя об отклонении части
    try:
        await bot.send_message(
            user_id,
            f"❌ Часть {part_index} твоей истории не прошла модерацию.\n"
            f"Ты можешь прислать исправленный вариант этой части."
        )
    except Exception as e:
        print(f"Не удалось уведомить пользователя: {e}")


# 🔥 ПОКАЗАТЬ ВСЕ ЧАСТИ
@router.callback_query(F.data.startswith("show_parts:"))
async def cb_show_parts(call: CallbackQuery):
    await call.answer()
    
    try:
        user_id = int(call.data.split(":")[1])
    except:
        await call.message.answer("❌ Ошибка в данных")
        return
    
    if user_id not in user_stories or not user_stories[user_id]:
        await call.answer("❌ Нет сохраненных частей")
        return
    
    parts_info = []
    for part in user_stories[user_id]:
        status = "✅" if part.get('status') == 'approved' else "⏳"
        parts_info.append(f"{status} Часть {part['index']}: {len(part['text'])} символов")
    
    summary = f"📚 Все части от user_id={user_id}:\n\n" + "\n".join(parts_info)
    await call.message.answer(summary)


# 🔥 ПУБЛИКАЦИЯ ВСЕХ ЧАСТЕЙ
@router.callback_query(F.data.startswith("publish_all:"))
async def cb_publish_all(call: CallbackQuery):
    await call.answer("🔄 Публикую...")
    
    try:
        user_id = int(call.data.split(":")[1])
    except:
        await call.message.answer("❌ Ошибка в данных")
        return
    
    # Публикуем все части
    success = await publish_all_parts(user_id)
    
    if success:
        # Помечаем все сообщения модерации как опубликованные
        current_text = call.message.caption or call.message.text or ""
        new_text = current_text + "\n\n🚀 <b>ВСЕ ЧАСТИ ОПУБЛИКОВАНЫ</b>"
        
        try:
            if call.message.photo:
                await call.message.edit_caption(new_text)
            else:
                await call.message.edit_text(new_text)
        except Exception as e:
            print(f"Ошибка редактирования: {e}")
        
        await call.message.answer(f"✅ Все части истории от user_id={user_id} опубликованы!")
    else:
        await call.message.answer(f"❌ Не удалось опубликовать историю user_id={user_id}")


# ---------- ЗАПУСК ----------

async def main():
    print("🤖 Bot started polling...")
    print(f"📺 Канал ID: {CHANNEL_ID}")
    print(f"🛡️ Модерация ID: {MOD_CHAT_ID or 'НЕ ЗАДАН'}")
    print(f"🔗 Система частей: ВКЛЮЧЕНА (макс {USER_BUFFER_SIZE} частей)")
    print("✅ ГОТОВ К РАБОТЕ!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
