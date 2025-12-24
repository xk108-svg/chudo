import asyncio
import os
import time
import re
from dataclasses import dataclass
from typing import Optional, Dict, List
from collections import defaultdict
from datetime import datetime

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


# 🔥 ГЛОБАЛЬНЫЕ БУФЕРЫ
user_stories: Dict[int, List[Dict]] = {}
USER_BUFFER_SIZE = 10

# 🔥 ДЛЯ СТАРОЙ СИСТЕМЫ МОДЕРАЦИИ
moderation_messages: Dict[int, Dict[int, int]] = defaultdict(dict)


# ---------- НАСТРОЙКИ ПРОЕКТА ----------

ADMIN_USER_ID = 318289611
LIMIT_SECONDS = 2 * 24 * 60 * 60

# user_id -> timestamp последней отправленной истории
last_story_ts: Dict[int, float] = {}


# ---------- МОДЕЛЬ ИСТОРИИ ----------

@dataclass
class StoryPart:
    id: Optional[int]
    index: int
    user_id: int
    username: str
    text: str
    status: str = "pending"
    type: str = "text"
    photo_file_id: Optional[str] = None
    message_id: Optional[int] = None
    timestamp: float = None
    channel_message_id: Optional[int] = None


# ---------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ----------

BOT_TOKEN = os.getenv("BOT_TOKEN")
MOD_CHAT_ID_RAW = os.getenv("MOD_CHAT_ID")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
COMMENTS_CHANNEL = "@comments_group_108"  # Канал для обсуждений
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print("=" * 50)
print("🤖 ЗАГРУЗКА БОТА")
print("=" * 50)
print("ENV BOT_TOKEN:", "✅ ЗАДАН" if BOT_TOKEN else "❌ НЕ ЗАДАН")
print("ENV MOD_CHAT_ID:", MOD_CHAT_ID_RAW if MOD_CHAT_ID_RAW else "❌ НЕ ЗАДАН")
print("ENV CHANNEL_ID:", CHANNEL_ID_RAW if CHANNEL_ID_RAW else "❌ НЕ ЗАДАН")
print("Канал обсуждений:", COMMENTS_CHANNEL)
print("=" * 50)

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан!")

if not CHANNEL_ID_RAW:
    raise ValueError("❌ CHANNEL_ID не задан!")

try:
    CHANNEL_ID = int(CHANNEL_ID_RAW)
except ValueError:
    raise ValueError(f"❌ Неверный формат CHANNEL_ID: {CHANNEL_ID_RAW}")

if MOD_CHAT_ID_RAW:
    try:
        MOD_CHAT_ID = int(MOD_CHAT_ID_RAW)
    except ValueError:
        print(f"⚠️ Неверный формат MOD_CHAT_ID: {MOD_CHAT_ID_RAW}")
        MOD_CHAT_ID = None
else:
    MOD_CHAT_ID = None
    print("⚠️ MOD_CHAT_ID не задан — модерация в чат отключена")

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
                print(f"❌ Supabase error {resp.status}: {data}")
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
        "channel_message_id": part.channel_message_id,
    }
    data = await supabase_request("POST", "/rest/v1/story_parts", json=payload)
    if not data:
        return None
    try:
        return data[0]["id"]
    except Exception as e:
        print(f"❌ Parse Supabase insert response error:", e, data)
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


def post_keyboard(channel_message_id: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура для поста в канале (ссылка на обсуждение)"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Обсудить в группе",
                    url=f"https://t.me/comments_group_108"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Поделись своей историей",
                    url="https://t.me/pishiistorii_bot"
                )
            ]
        ]
    )


async def send_part_to_moderation(part_data: dict, is_first: bool = False):
    """Отправляет часть истории в чат модерации"""
    if not MOD_CHAT_ID:
        print("❌ MOD_CHAT_ID не задан, модерация невозможна")
        return None
    
    user_id = part_data['user_id']
    index = part_data['index']
    text = part_data['text']
    username = part_data['username']
    photo_file_id = part_data.get('photo')
    timestamp = part_data['timestamp']
    
    header = (
        f"📝 Часть {index}\n"
        f"Автор: @{username} (id {user_id})\n"
        f"Время: {datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')}\n"
    )
    
    if is_first:
        header += "🆕 НАЧАЛО НОВОЙ ИСТОРИИ\n\n"
    
    full_text = header + (text if text else "")
    
    try:
        if photo_file_id:
            msg = await bot.send_photo(
                MOD_CHAT_ID,
                photo=photo_file_id,
                caption=full_text if text else header,
                reply_markup=moderation_keyboard(user_id, index),
                parse_mode=ParseMode.HTML,
            )
        else:
            msg = await bot.send_message(
                MOD_CHAT_ID,
                full_text,
                reply_markup=moderation_keyboard(user_id, index),
                parse_mode=ParseMode.HTML,
            )
        
        if user_id not in moderation_messages:
            moderation_messages[user_id] = {}
        moderation_messages[user_id][index] = msg.message_id
        
        return msg.message_id
        
    except Exception as e:
        print(f"❌ Ошибка отправки в модерацию: {e}")
        return None


async def publish_single_post(text: str, photo_file_id: Optional[str], username: str, user_id: int) -> int:
    """Публикует одиночный пост в канал и возвращает message_id"""
    try:
        # Добавляем приписку для реакций
        reaction_text = "\n\n🙏 ❤️ 👍 ✨ 🙌"
        full_text = text + reaction_text
        
        if photo_file_id:
            msg = await bot.send_photo(
                CHANNEL_ID,
                photo=photo_file_id,
                caption=full_text if text else None,
                reply_markup=post_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        else:
            msg = await bot.send_message(
                CHANNEL_ID,
                full_text,
                reply_markup=post_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        
        print(f"✅ Опубликован одиночный пост от user_id={user_id}, message_id={msg.message_id}")
        return msg.message_id
        
    except Exception as e:
        print(f"❌ Ошибка публикации одиночного поста: {e}")
        raise


async def publish_all_parts(user_id: int) -> List[int]:
    """Публикует все одобренные части в канал, возвращает список message_id"""
    if user_id not in user_stories:
        print(f"❌ Нет частей для публикации user_id={user_id}")
        return []
    
    parts = user_stories[user_id]
    if not parts:
        return []
    
    # Сортируем части по индексу
    sorted_parts = sorted(parts, key=lambda x: x.get('index', 0))
    
    print(f"🚀 Публикация {len(sorted_parts)} частей от user_id={user_id}")
    
    published_message_ids = []
    
    for part in sorted_parts:
        try:
            text = part['text']
            photo_file_id = part.get('photo')
            
            # Добавляем номер части в начало
            part_header = f"<b>Часть {part['index']}</b>\n\n" if len(sorted_parts) > 1 else ""
            
            # Для последней части добавляем приписку для реакций
            is_last_part = (part['index'] == len(sorted_parts))
            reaction_text = "\n\n🙏 ❤️ 👍 ✨ 🙌" if is_last_part else ""
            
            full_text = part_header + text + reaction_text
            
            # Для последней части добавляем клавиатуру
            if is_last_part:
                if photo_file_id:
                    msg = await bot.send_photo(
                        CHANNEL_ID,
                        photo=photo_file_id,
                        caption=full_text if text else None,
                        reply_markup=post_keyboard(),
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    msg = await bot.send_message(
                        CHANNEL_ID,
                        full_text,
                        reply_markup=post_keyboard(),
                        parse_mode=ParseMode.HTML,
                    )
            else:
                if photo_file_id:
                    msg = await bot.send_photo(
                        CHANNEL_ID,
                        photo=photo_file_id,
                        caption=full_text if text else None,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    msg = await bot.send_message(
                        CHANNEL_ID,
                        full_text,
                        parse_mode=ParseMode.HTML,
                    )
            
            published_message_ids.append(msg.message_id)
            print(f"✅ Опубликована часть {part['index']}, message_id={msg.message_id}")
            
            if len(published_message_ids) < len(sorted_parts):
                await asyncio.sleep(0.5)
                
        except Exception as e:
            print(f"❌ Ошибка публикации части {part.get('index')}: {e}")
    
    # Очищаем буфер
    if user_id in user_stories:
        del user_stories[user_id]
    
    # Уведомляем автора
    try:
        await bot.send_message(
            chat_id=user_id,
            text=f"✨ Твоя история ({len(sorted_parts)} частей) опубликована в канале!\n"
                 f"Под ней есть кнопка для обсуждения в группе.",
        )
        print(f"✅ Уведомлён автор {user_id}")
    except Exception as e:
        print(f"❌ Не удалось уведомить автора: {e}")
    
    return published_message_ids


# ---------- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЬЯ ----------

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    start_text = (
        "🌟 Добро пожаловать в бот историй!\n\n"
        "📝 Здесь ты можешь поделиться своей историей.\n"
        "🎯 После публикации в канале:\n"
        "• Нажми на эмодзи под постом (🙏 ❤️ 👍 ✨ 🙌)\n"
        "• Нажми на кнопку 💬 для обсуждения в группе\n\n"
        "Присылай свою историю - она появится в канале после модерации!"
    )
    await message.answer(start_text)


@router.message(F.text.startswith("/ad "))
async def cmd_ad(message: Message):
    """Реклама"""
    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1]
        ad_text = message.text[4:].strip()
        
        if not ad_text:
            await message.answer("❌ После /ad напиши текст объявления.")
            return
            
        reaction_text = "\n\n🙏 ❤️ 👍 ✨ 🙌"
        full_ad_text = ad_text + reaction_text
        
        await bot.send_photo(
            CHANNEL_ID,
            photo=photo.file_id,
            caption=f"📢 <b>Реклама</b>\n\n{full_ad_text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✍️ Поделись своей историей",
                    url="https://t.me/pishiistorii_bot"
                )]
            ]),
            parse_mode=ParseMode.HTML,
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

    reaction_text = "\n\n🙏 ❤️ 👍 ✨ 🙌"
    full_ad_text = ad_text + reaction_text
    
    await bot.send_message(
        CHANNEL_ID,
        f"📢 <b>Реклама</b>\n\n{full_ad_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✍️ Поделись своей историей",
                url="https://t.me/pishiistorii_bot"
            )]
        ]),
        parse_mode=ParseMode.HTML,
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
    """Обработка историй от пользователей"""
    user = message.from_user
    user_id = user.id
    username = user.username or "anon"
    
    print(f"📨 Получено сообщение от {user_id} (@{username})")

    # Получаем данные из сообщения
    text = message.caption or message.text or ""
    has_photo = message.photo is not None
    photo_file_id = message.photo[-1].file_id if has_photo else None
    
    # Ограничение по времени для обычных пользователей
    if user.id != ADMIN_USER_ID:
        now = time.time()
        last_ts = last_story_ts.get(user_id)
        
        # Проверяем, начинается ли новая история
        is_new_story = False
        if user_id in user_stories and user_stories[user_id]:
            last_part_time = user_stories[user_id][-1].get('timestamp', 0)
            if now - last_part_time > 300:  # 5 минут
                is_new_story = True
                print(f"🆕 Новая история для user_id={user_id}")
                if user_id in user_stories:
                    del user_stories[user_id]
        
        if last_ts and now - last_ts < LIMIT_SECONDS and is_new_story:
            hours_left = int((LIMIT_SECONDS - (now - last_ts)) // 3600) + 1
            await message.answer(
                f"⏳ Ты уже делился историей недавно.\n"
                f"Пожалуйста, приходи с новой историей через примерно {hours_left} ч."
            )
            return
        
        if is_new_story:
            last_story_ts[user_id] = now

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
        'username': username,
        'user_id': user_id,
        'timestamp': time.time(),
        'status': 'pending',
        'type': 'photo' if has_photo else 'text'
    }
    user_stories[user_id].append(story_part)
    
    print(f"📚 Сохранена часть {part_index} от user_id={user_id}")
    
    # Отправляем в модерацию
    if MOD_CHAT_ID:
        moderation_msg_id = await send_part_to_moderation(
            story_part,
            is_first=(part_index == 1)
        )
    else:
        moderation_msg_id = None
    
    # Сохраняем в Supabase
    if SUPABASE_ENABLED:
        part_obj = StoryPart(
            id=None,
            index=part_index,
            user_id=user_id,
            username=username,
            text=text,
            status='pending',
            type='photo' if has_photo else 'text',
            photo_file_id=photo_file_id,
            message_id=moderation_msg_id,
            timestamp=time.time()
        )
        await save_story_part_to_supabase(part_obj)
    
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


# ---------- ОБРАБОТЧИКИ МОДЕРАЦИИ ----------

@router.callback_query(F.data.startswith("approve_part:"))
async def cb_approve_part(call: CallbackQuery):
    """Одобрение отдельной части"""
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
                print(f"📊 Статус части {part_index} обновлен на 'approved'")
                break
    
    # Помечаем в модерации
    current_text = call.message.caption or call.message.text or ""
    new_text = current_text + "\n\n✅ <b>Часть одобрена</b>"
    
    try:
        if call.message.photo:
            await call.message.edit_caption(new_text, parse_mode=ParseMode.HTML)
        else:
            await call.message.edit_text(new_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Ошибка редактирования: {e}")
    
    # Проверяем, все ли части одобрены
    if user_id in user_stories and user_stories[user_id]:
        all_approved = all(part.get('status') == 'approved' for part in user_stories[user_id])
        total_parts = len(user_stories[user_id])
        approved_parts = sum(1 for part in user_stories[user_id] if part.get('status') == 'approved')
        
        print(f"📊 Проверка одобрения: {approved_parts}/{total_parts} частей одобрено")
        
        if all_approved:
            await call.message.answer(
                f"🎉 Все {total_parts} части истории от user_id={user_id} одобрены!\n"
                f"Нажмите '🚀 Опубликовать всё' для публикации."
            )


@router.callback_query(F.data.startswith("publish_all:"))
async def cb_publish_all(call: CallbackQuery):
    """Публикация всех частей"""
    await call.answer("🔄 Публикую...")
    
    try:
        user_id = int(call.data.split(":")[1])
    except:
        await call.message.answer("❌ Ошибка в данных")
        return
    
    # Публикуем все части
    message_ids = await publish_all_parts(user_id)
    
    if message_ids:
        # Помечаем сообщение модерации как опубликованное
        current_text = call.message.caption or call.message.text or ""
        new_text = current_text + "\n\n🚀 <b>ВСЕ ЧАСТИ ОПУБЛИКОВАНЫ</b>"
        
        try:
            if call.message.photo:
                await call.message.edit_caption(new_text, parse_mode=ParseMode.HTML)
            else:
                await call.message.edit_text(new_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"Ошибка редактирования: {e}")
        
        await call.message.answer(f"✅ Все части истории от user_id={user_id} опубликованы!")
    else:
        await call.message.answer(f"❌ Не удалось опубликовать историю user_id={user_id}")


@router.callback_query(F.data.startswith("approve:"))
async def cb_approve_single(call: CallbackQuery):
    """Одобрение одиночного поста"""
    await call.answer("✅ Пост одобрен и опубликован!")
    
    try:
        user_id = int(call.data.split(":")[1])
    except:
        await call.message.answer("❌ Ошибка в данных")
        return
    
    print(f"✅ Одобрен одиночный пост от user_id={user_id}")
    
    # Получаем текст и фото из сообщения модерации
    message = call.message
    text = message.caption or message.text or ""
    photo_file_id = None
    
    # Извлекаем user_id из текста
    extracted_user_id = extract_user_id_from_moderation_text(text)
    if extracted_user_id:
        user_id = extracted_user_id
    
    # Убираем заголовок модерации
    lines = text.split('\n')
    content_start = 0
    for i, line in enumerate(lines):
        if line.startswith('Время:'):
            content_start = i + 1
            break
    
    # Берем только контент
    content_lines = lines[content_start:]
    clean_text = '\n'.join(content_lines).strip()
    
    # Проверяем, есть ли фото
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    
    # Публикуем в канал
    try:
        channel_message_id = await publish_single_post(clean_text, photo_file_id, "user", user_id)
        
        # Помечаем в модерации
        current_text = message.caption or message.text or ""
        new_text = current_text + "\n\n✅ <b>Одобрено и опубликовано!</b>"
        
        try:
            if message.photo:
                await message.edit_caption(new_text, parse_mode=ParseMode.HTML)
            else:
                await message.edit_text(new_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"Ошибка редактирования модерации: {e}")
        
        # Уведомляем автора
        try:
            await bot.send_message(
                chat_id=user_id,
                text="✨ Твоя история прошла модерацию и опубликована в канале!\n"
                     f"Под ней есть кнопка для обсуждения в группе.",
            )
            print(f"✅ Уведомлён автор {user_id}")
        except Exception as e:
            print(f"❌ Не удалось уведомить автора: {e}")
            
    except Exception as e:
        print(f"❌ Ошибка публикации: {e}")
        await call.message.answer(f"❌ Ошибка публикации: {e}")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject_single(call: CallbackQuery):
    """Отклонение одиночного поста"""
    await call.answer("❌ Пост отклонен")
    
    try:
        user_id = int(call.data.split(":")[1])
    except:
        await call.message.answer("❌ Ошибка в данных")
        return
    
    print(f"❌ Отклонен одиночный пост от user_id={user_id}")
    
    # Помечаем в модерации
    current_text = call.message.caption or call.message.text or ""
    new_text = current_text + "\n\n❌ <b>Отклонено</b>"
    
    try:
        if call.message.photo:
            await call.message.edit_caption(new_text, parse_mode=ParseMode.HTML)
        else:
            await call.message.edit_text(new_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Ошибка редактирования модерации: {e}")
    
    # Уведомляем автора
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "Твоя история не прошла модерацию и не была опубликована.\n\n"
                "Проверь, пожалуйста, чтобы не было политики, брани и оскорблений, "
                "и попробуй пересказать её чуть мягче."
            ),
        )
    except Exception as e:
        print("Cannot notify user:", e)


@router.callback_query(F.data.startswith("reject_part:"))
async def cb_reject_part(call: CallbackQuery):
    """Отклонение части"""
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
            await call.message.edit_caption(new_text, parse_mode=ParseMode.HTML)
        else:
            await call.message.edit_text(new_text, parse_mode=ParseMode.HTML)
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


@router.callback_query(F.data.startswith("show_parts:"))
async def cb_show_parts(call: CallbackQuery):
    """Показать все части"""
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
        text_preview = part['text'][:50] + "..." if len(part['text']) > 50 else part['text']
        parts_info.append(f"{status} Часть {part['index']}: {text_preview}")
    
    summary = f"📚 Все части от user_id={user_id}:\n\n" + "\n".join(parts_info)
    await call.message.answer(summary)


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def extract_user_id_from_moderation_text(text: str) -> Optional[int]:
    """Ищет в тексте строку вида '(id 123456789)' и возвращает число."""
    m = re.search(r"\(id (\d+)\)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


# ---------- ЗАПУСК ----------

async def main():
    print("=" * 50)
    print("🤖 БОТ ЗАПУЩЕН")
    print("=" * 50)
    print(f"📺 Канал публикаций: {CHANNEL_ID}")
    print(f"💬 Канал обсуждений: {COMMENTS_CHANNEL}")
    print(f"🛡️ Модерация ID: {MOD_CHAT_ID or 'НЕ ЗАДАН'}")
    print("=" * 50)
    print("✅ ГОТОВ К РАБОТЕ!")
    print("=" * 50)
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        print("=" * 50)
        print("🛠️ ПРОВЕРЬТЕ:")
        print("1. Переменные окружения (BOT_TOKEN, CHANNEL_ID)")
        print("2. Интернет-соединение")
        print("3. Права бота (должен быть администратором в канале)")
        print("=" * 50)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем")
    except Exception as e:
        print(f"\n❌ Неожиданная ошибка: {e}")
