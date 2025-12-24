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
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
import aiohttp


# 🔥 ГЛОБАЛЬНЫЕ БУФЕРЫ
user_stories: Dict[int, List[Dict]] = {}
USER_BUFFER_SIZE = 10

# 🔥 СИСТЕМА КОММЕНТАРИЕВ И РЕЙТИНГА
# channel_message_id -> {user_id: rating, comments: []}
post_ratings: Dict[int, Dict] = defaultdict(lambda: {
    'ratings': {},
    'comments': [],
    'total_score': 0,
    'rating_count': 0
})

# user_id -> {channel_message_id: comment_text}
user_comments: Dict[int, Dict[int, str]] = defaultdict(dict)


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
    channel_message_id: Optional[int] = None  # ID сообщения в канале


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


# ---------- ФУНКЦИИ ДЛЯ РЕЙТИНГА И КОММЕНТАРИЕВ ----------

def rating_keyboard(channel_message_id: int, user_rating: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура для оценки поста"""
    stars = []
    for i in range(1, 6):
        if user_rating >= i:
            stars.append(InlineKeyboardButton(text="⭐", callback_data=f"rate:{channel_message_id}:{i}"))
        else:
            stars.append(InlineKeyboardButton(text="☆", callback_data=f"rate:{channel_message_id}:{i}"))
    
    return InlineKeyboardMarkup(
        inline_keyboard=[
            stars,
            [
                InlineKeyboardButton(
                    text="💬 Комментировать",
                    callback_data=f"comment:{channel_message_id}"
                ),
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data=f"stats:{channel_message_id}"
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


def comment_confirmation_keyboard(channel_message_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для подтверждения комментария"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить",
                    callback_data=f"send_comment:{channel_message_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"cancel_comment:{channel_message_id}"
                )
            ]
        ]
    )


def stats_keyboard(channel_message_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для статистики"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к посту",
                    callback_data=f"back_to_post:{channel_message_id}"
                )
            ]
        ]
    )


async def update_post_with_rating(channel_message_id: int):
    """Обновляет пост в канале с текущим рейтингом"""
    if channel_message_id not in post_ratings:
        return
    
    post_data = post_ratings[channel_message_id]
    rating_count = post_data['rating_count']
    total_score = post_data['total_score']
    
    if rating_count > 0:
        avg_rating = total_score / rating_count
        rating_text = f"\n\n⭐ Рейтинг: {avg_rating:.1f}/5 ({rating_count} оценок)"
        
        # Получаем текущее сообщение
        try:
            message = await bot.get_message(CHANNEL_ID, channel_message_id)
            current_text = message.caption or message.text
            
            # Убираем старый рейтинг если есть
            lines = current_text.split('\n')
            if '⭐ Рейтинг:' in lines[-1]:
                lines = lines[:-1]
            
            new_text = '\n'.join(lines) + rating_text
            
            # Обновляем сообщение
            if message.photo:
                await message.edit_caption(new_text, reply_markup=rating_keyboard(channel_message_id))
            else:
                await message.edit_text(new_text, reply_markup=rating_keyboard(channel_message_id))
                
        except Exception as e:
            print(f"Ошибка обновления рейтинга: {e}")


async def send_comment_notification(channel_message_id: int, user_id: int, comment: str, username: str):
    """Отправляет уведомление о новом комментарии в чат модерации"""
    if MOD_CHAT_ID:
        try:
            # Получаем ссылку на пост
            post_link = f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{channel_message_id}"
            
            notification = (
                f"💬 Новый комментарий\n"
                f"К посту: {post_link}\n"
                f"От: @{username} (id {user_id})\n"
                f"Комментарий: {comment}"
            )
            
            await bot.send_message(MOD_CHAT_ID, notification)
        except Exception as e:
            print(f"Ошибка отправки уведомления о комментарии: {e}")


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
        "channel_message_id": part.channel_message_id,
    }
    data = await supabase_request("POST", "/rest/v1/story_parts", json=payload)
    if not data:
        return None
    try:
        return data[0]["id"]
    except Exception as e:
        print("Parse Supabase insert response error:", e, data)
        return None


async def save_rating_to_supabase(channel_message_id: int, user_id: int, rating: int):
    """Сохраняет оценку в Supabase"""
    if not SUPABASE_ENABLED:
        return None
    
    payload = {
        "channel_message_id": channel_message_id,
        "user_id": user_id,
        "rating": rating,
        "timestamp": time.time(),
    }
    return await supabase_request("POST", "/rest/v1/ratings", json=payload)


async def save_comment_to_supabase(channel_message_id: int, user_id: int, username: str, comment: str):
    """Сохраняет комментарий в Supabase"""
    if not SUPABASE_ENABLED:
        return None
    
    payload = {
        "channel_message_id": channel_message_id,
        "user_id": user_id,
        "username": username,
        "comment": comment,
        "timestamp": time.time(),
    }
    return await supabase_request("POST", "/rest/v1/comments", json=payload)


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
            full_text = part_header + text
            
            # Для последней части добавляем начальную клавиатуру с оценкой
            is_last_part = (part['index'] == len(sorted_parts))
            
            if photo_file_id:
                if is_last_part:
                    msg = await bot.send_photo(
                        CHANNEL_ID,
                        photo=photo_file_id,
                        caption=full_text if text else None,
                        reply_markup=rating_keyboard(0),  # 0 - временный ID
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    msg = await bot.send_photo(
                        CHANNEL_ID,
                        photo=photo_file_id,
                        caption=full_text if text else None,
                        parse_mode=ParseMode.HTML,
                    )
            else:
                if is_last_part:
                    msg = await bot.send_message(
                        CHANNEL_ID,
                        full_text,
                        reply_markup=rating_keyboard(0),  # 0 - временный ID
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
            
            # Сохраняем channel_message_id в буфере
            part['channel_message_id'] = msg.message_id
            
            if published_message_ids < len(sorted_parts):
                await asyncio.sleep(0.5)
                
        except Exception as e:
            print(f"❌ Ошибка публикации части {part.get('index')}: {e}")
    
    # Для последнего сообщения обновляем клавиатуру с правильным message_id
    if published_message_ids:
        last_message_id = published_message_ids[-1]
        
        # Инициализируем запись рейтинга
        post_ratings[last_message_id] = {
            'ratings': {},
            'comments': [],
            'total_score': 0,
            'rating_count': 0
        }
        
        # Обновляем клавиатуру с правильным ID
        try:
            last_message = await bot.get_message(CHANNEL_ID, last_message_id)
            if last_message.photo:
                await last_message.edit_reply_markup(reply_markup=rating_keyboard(last_message_id))
            else:
                await last_message.edit_reply_markup(reply_markup=rating_keyboard(last_message_id))
        except Exception as e:
            print(f"Ошибка обновления клавиатуры: {e}")
    
    # Очищаем буфер
    if user_id in user_stories:
        del user_stories[user_id]
    
    # Уведомляем автора
    try:
        await bot.send_message(
            chat_id=user_id,
            text=f"✨ Твоя история ({len(sorted_parts)} частей) опубликована в канале!\n"
                 f"Под ней есть кнопки для оценки и комментариев.",
        )
        print(f"✅ Уведомлён автор {user_id}")
    except Exception as e:
        print(f"❌ Не удалось уведомить автора: {e}")
    
    return published_message_ids


# ---------- ХЕНДЛЕРЫ РЕЙТИНГА И КОММЕНТАРИЕВ ----------

@router.callback_query(F.data.startswith("rate:"))
async def cb_rate(call: CallbackQuery):
    """Обработка оценки поста"""
    await call.answer()
    
    try:
        _, channel_msg_id_str, rating_str = call.data.split(":")
        channel_message_id = int(channel_msg_id_str)
        rating = int(rating_str)
    except:
        return
    
    user_id = call.from_user.id
    
    # Сохраняем оценку
    if channel_message_id in post_ratings:
        post_data = post_ratings[channel_message_id]
        
        # Если пользователь уже оценивал, убираем старую оценку
        if user_id in post_data['ratings']:
            old_rating = post_data['ratings'][user_id]
            post_data['total_score'] -= old_rating
            post_data['rating_count'] -= 1
        
        # Добавляем новую оценку
        post_data['ratings'][user_id] = rating
        post_data['total_score'] += rating
        post_data['rating_count'] += 1
        
        # Обновляем пост
        await update_post_with_rating(channel_message_id)
        
        # Сохраняем в Supabase
        if SUPABASE_ENABLED:
            await save_rating_to_supabase(channel_message_id, user_id, rating)
        
        # Уведомляем пользователя
        await call.answer(f"Спасибо! Вы поставили {rating} ⭐")
    else:
        await call.answer("❌ Пост не найден")


@router.callback_query(F.data.startswith("comment:"))
async def cb_start_comment(call: CallbackQuery):
    """Начало написания комментария"""
    await call.answer()
    
    try:
        channel_message_id = int(call.data.split(":")[1])
    except:
        return
    
    # Просим пользователя написать комментарий
    await call.message.answer(
        "💬 Напишите ваш комментарий к этому посту:",
        reply_markup=ReplyKeyboardRemove()
    )
    
    # Сохраняем информацию о том, что пользователь пишет комментарий
    if call.from_user.id not in user_comments:
        user_comments[call.from_user.id] = {}
    user_comments[call.from_user.id][channel_message_id] = ""
    
    # Отправляем кнопку отмены
    await call.message.answer(
        "Отправьте сообщение с комментарием или нажмите кнопку для отмены:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"cancel_comment:{channel_message_id}"
                )]
            ]
        )
    )


@router.message(F.text & ~F.text.startswith(("/", "start", "ad")))
async def handle_comment_text(message: Message):
    """Обработка текста комментария"""
    user_id = message.from_user.id
    
    # Проверяем, пишет ли пользователь комментарий
    if user_id in user_comments and user_comments[user_id]:
        # Берем первый channel_message_id из словаря
        channel_message_id = next(iter(user_comments[user_id].keys()))
        
        # Сохраняем текст комментария
        user_comments[user_id][channel_message_id] = message.text
        
        # Просим подтвердить отправку
        await message.answer(
            f"💬 Ваш комментарий:\n\n{message.text}\n\nОтправить?",
            reply_markup=comment_confirmation_keyboard(channel_message_id)
        )
    else:
        # Это не комментарий, обрабатываем как обычно
        await handle_story(message)


@router.callback_query(F.data.startswith("send_comment:"))
async def cb_send_comment(call: CallbackQuery):
    """Отправка комментария"""
    await call.answer("💬 Комментарий отправлен!")
    
    try:
        channel_message_id = int(call.data.split(":")[1])
    except:
        return
    
    user_id = call.from_user.id
    username = call.from_user.username or "anon"
    
    if user_id in user_comments and channel_message_id in user_comments[user_id]:
        comment_text = user_comments[user_id][channel_message_id]
        
        if comment_text:
            # Сохраняем комментарий
            if channel_message_id in post_ratings:
                post_ratings[channel_message_id]['comments'].append({
                    'user_id': user_id,
                    'username': username,
                    'text': comment_text,
                    'timestamp': time.time()
                })
            
            # Сохраняем в Supabase
            if SUPABASE_ENABLED:
                await save_comment_to_supabase(channel_message_id, user_id, username, comment_text)
            
            # Отправляем уведомление в модерацию
            await send_comment_notification(channel_message_id, user_id, comment_text, username)
            
            # Уведомляем пользователя
            await call.message.answer("✅ Ваш комментарий сохранен и отправлен на модерацию.")
        
        # Очищаем временные данные
        if user_id in user_comments:
            if channel_message_id in user_comments[user_id]:
                del user_comments[user_id][channel_message_id]
            if not user_comments[user_id]:
                del user_comments[user_id]
    else:
        await call.answer("❌ Комментарий не найден")


@router.callback_query(F.data.startswith("cancel_comment:"))
async def cb_cancel_comment(call: CallbackQuery):
    """Отмена комментария"""
    await call.answer("❌ Комментарий отменен")
    
    try:
        channel_message_id = int(call.data.split(":")[1])
    except:
        return
    
    user_id = call.from_user.id
    
    # Очищаем временные данные
    if user_id in user_comments and channel_message_id in user_comments[user_id]:
        del user_comments[user_id][channel_message_id]
        if not user_comments[user_id]:
            del user_comments[user_id]
    
    await call.message.answer("Комментарий отменен.")


@router.callback_query(F.data.startswith("stats:"))
async def cb_show_stats(call: CallbackQuery):
    """Показать статистику поста"""
    await call.answer()
    
    try:
        channel_message_id = int(call.data.split(":")[1])
    except:
        return
    
    if channel_message_id not in post_ratings:
        await call.message.answer("Статистика пока недоступна для этого поста.")
        return
    
    post_data = post_ratings[channel_message_id]
    rating_count = post_data['rating_count']
    total_score = post_data['total_score']
    comment_count = len(post_data['comments'])
    
    if rating_count > 0:
        avg_rating = total_score / rating_count
        rating_text = f"⭐ Средний рейтинг: {avg_rating:.1f}/5\n👥 Количество оценок: {rating_count}"
    else:
        rating_text = "⭐ Пока нет оценок"
    
    stats_text = (
        f"📊 Статистика поста:\n\n"
        f"{rating_text}\n"
        f"💬 Комментариев: {comment_count}"
    )
    
    await call.message.answer(stats_text, reply_markup=stats_keyboard(channel_message_id))


@router.callback_query(F.data.startswith("back_to_post:"))
async def cb_back_to_post(call: CallbackQuery):
    """Вернуться к посту"""
    await call.answer()
    
    try:
        channel_message_id = int(call.data.split(":")[1])
    except:
        return
    
    # Получаем ссылку на пост
    post_link = f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{channel_message_id}"
    
    await call.message.answer(
        f"🔗 Ссылка на пост: {post_link}\n\n"
        f"Вы можете вернуться в канал, чтобы оценить пост или оставить комментарий.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="📋 Открыть пост",
                    url=post_link
                )]
            ]
        )
    )


# ---------- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЬЯ ----------

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    start_text = (
        "🌟 Добро пожаловать в бот историй!\n\n"
        "📝 Здесь ты можешь поделиться своей историей.\n"
        "🎯 В канале под каждой историей есть кнопки:\n"
        "• ⭐ - Оценить историю (от 1 до 5 звезд)\n"
        "• 💬 - Оставить комментарий\n"
        "• 📊 - Посмотреть статистику\n\n"
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

    await bot.send_message(
        CHANNEL_ID,
        f"📢 <b>Реклама</b>\n\n{ad_text}",
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
    
    # Проверяем, не пишет ли пользователь комментарий
    if user_id in user_comments and user_comments[user_id]:
        # Это комментарий, обрабатываем в другом месте
        return
    
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


# ---------- ОБРАБОТЧИКИ МОДЕРАЦИИ (остаются как были) ----------

# ... (оставьте обработчики cb_approve_part, cb_reject_part, cb_show_parts, cb_publish_all без изменений)
# Они остаются такими же, как в предыдущей версии кода


# ---------- ЗАПУСК ----------

async def main():
    print("🤖 Bot started polling...")
    print(f"📺 Канал ID: {CHANNEL_ID}")
    print(f"🛡️ Модерация ID: {MOD_CHAT_ID or 'НЕ ЗАДАН'}")
    print(f"⭐ Система оценок и комментариев: ВКЛЮЧЕНА")
    print("✅ ГОТОВ К РАБОТЕ!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
