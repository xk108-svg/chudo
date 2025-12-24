import asyncio
import os
import time
import re
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


# 🔥 БУФЕР ДЛЯ ПОЛЬЗОВАТЕЛЕЙ
user_messages: Dict[int, List[Dict]] = defaultdict(list)  # user_id -> список сообщений


# ---------- НАСТРОЙКИ ----------

ADMIN_USER_ID = 318289611  # Ваш ID
LIMIT_SECONDS = 2 * 24 * 60 * 60  # 2 дня между историями
last_story_time: Dict[int, float] = {}  # user_id -> время последней истории


# ---------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ----------

BOT_TOKEN = os.getenv("BOT_TOKEN")
MOD_CHAT_ID_RAW = os.getenv("MOD_CHAT_ID")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
COMMENTS_CHANNEL = "@comments_group_108"

print("=" * 50)
print("🤖 ЗАГРУЗКА БОТА")
print("=" * 50)

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан!")
if not CHANNEL_ID_RAW:
    raise ValueError("❌ CHANNEL_ID не задан!")

CHANNEL_ID = int(CHANNEL_ID_RAW)

if MOD_CHAT_ID_RAW:
    MOD_CHAT_ID = int(MOD_CHAT_ID_RAW)
else:
    MOD_CHAT_ID = None
    print("⚠️ MOD_CHAT_ID не задан — модерация отключена")

print(f"📺 Канал: {CHANNEL_ID}")
print(f"💬 Обсуждения: {COMMENTS_CHANNEL}")
print("=" * 50)


# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ---------- СЛУЖЕБНЫЕ ФУНКЦИИ ----------

def moderation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Кнопки для модерации всей истории пользователя"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Опубликовать всё",
                    callback_data=f"publish:{user_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить всё",
                    callback_data=f"reject:{user_id}"
                )
            ]
        ]
    )


def channel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура под постом в канале"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Обсудить",
                    url=f"https://t.me/comments_group_108"
                ),
                InlineKeyboardButton(
                    text="✍️ Поделись историей",
                    url="https://t.me/pishiistorii_bot"
                )
            ]
        ]
    )


async def send_to_moderation(user_id: int, username: str):
    """Отправляет все сообщения пользователя в чат модерации"""
    if not MOD_CHAT_ID:
        return
    
    if user_id not in user_messages or not user_messages[user_id]:
        return
    
    messages = user_messages[user_id]
    
    # Отправляем заголовок модерации
    header = await bot.send_message(
        MOD_CHAT_ID,
        f"📨 Новая история\n"
        f"Автор: @{username} (id {user_id})\n"
        f"Сообщений: {len(messages)}\n"
        f"Время: {datetime.now().strftime('%H:%M:%S')}\n"
        f"{'━' * 30}",
        reply_markup=moderation_keyboard(user_id)
    )
    
    # Отправляем все сообщения подряд
    for msg_data in messages:
        try:
            if msg_data.get('photo'):
                await bot.send_photo(
                    MOD_CHAT_ID,
                    photo=msg_data['photo'],
                    caption=msg_data.get('text', ''),
                )
            else:
                await bot.send_message(
                    MOD_CHAT_ID,
                    msg_data['text'],
                )
            await asyncio.sleep(0.1)  # Небольшая пауза
        except Exception as e:
            print(f"❌ Ошибка отправки в модерацию: {e}")
    
    return header.message_id


async def publish_to_channel(user_id: int):
    """Публикует все сообщения пользователя в канал"""
    if user_id not in user_messages or not user_messages[user_id]:
        return []
    
    messages = user_messages[user_id]
    published_ids = []
    
    print(f"🚀 Публикация {len(messages)} сообщений от user_id={user_id}")
    
    # Публикуем все сообщения
    for i, msg_data in enumerate(messages):
        try:
            is_last = (i == len(messages) - 1)
            text = msg_data.get('text', '')
            photo = msg_data.get('photo')
            
            # Для последнего сообщения добавляем реакции и кнопки
            if is_last:
                reactions = "\n\n🙏 ❤️ 👍 ✨ 🙌"
                full_text = text + reactions if text else reactions
                
                if photo:
                    msg = await bot.send_photo(
                        CHANNEL_ID,
                        photo=photo,
                        caption=full_text if full_text.strip() else None,
                        reply_markup=channel_keyboard(),
                    )
                else:
                    msg = await bot.send_message(
                        CHANNEL_ID,
                        full_text,
                        reply_markup=channel_keyboard(),
                    )
            else:
                if photo:
                    msg = await bot.send_photo(
                        CHANNEL_ID,
                        photo=photo,
                        caption=text if text else None,
                    )
                else:
                    msg = await bot.send_message(
                        CHANNEL_ID,
                        text,
                    )
            
            published_ids.append(msg.message_id)
            print(f"✅ Опубликовано сообщение {i+1}, msg_id={msg.message_id}")
            
            await asyncio.sleep(0.1)
            
        except Exception as e:
            print(f"❌ Ошибка публикации: {e}")
    
    # Очищаем буфер пользователя
    if user_id in user_messages:
        del user_messages[user_id]
    
    # Уведомляем автора
    try:
        await bot.send_message(
            chat_id=user_id,
            text="✨ Твоя история опубликована в канале!",
        )
    except Exception:
        pass
    
    return published_ids


# ---------- ХЕНДЛЕРЫ ----------

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    """Приветствие"""
    await message.answer(
        "🌟 Добро пожаловать!\n\n"
        "📝 Напиши свою историю и отправь её.\n"
        "Можешь добавить фото.\n\n"
        "📨 История отправится на модерацию и "
        "будет опубликована в канале после проверки."
    )


@router.message(F.text.startswith("/ad "))
async def cmd_ad(message: Message):
    """Реклама (только для админа)"""
    if message.from_user.id != ADMIN_USER_ID:
        return
    
    ad_text = message.text[4:].strip()
    if not ad_text:
        await message.answer("❌ Напиши текст рекламы")
        return
    
    reactions = "\n\n🙏 ❤️ 👍 ✨ 🙌"
    
    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1].file_id
        await bot.send_photo(
            CHANNEL_ID,
            photo=photo,
            caption=f"📢 <b>Реклама</b>\n\n{ad_text}{reactions}",
            reply_markup=channel_keyboard(),
        )
    else:
        await bot.send_message(
            CHANNEL_ID,
            f"📢 <b>Реклама</b>\n\n{ad_text}{reactions}",
            reply_markup=channel_keyboard(),
        )
    
    await message.answer("✅ Реклама опубликована")
    try:
        await message.delete()
    except:
        pass


# 🔥 ГЛАВНЫЙ ХЕНДЛЕР - ПРИЕМ СООБЩЕНИЙ
@router.message(
    (F.photo & ~F.reply_to_message) | 
    (F.text & ~F.text.startswith(("/ad", "/start")))
)
async def handle_message(message: Message):
    """Принимает все сообщения от пользователей"""
    user = message.from_user
    user_id = user.id
    username = user.username or "anon"
    
    print(f"📨 Получено сообщение от {user_id} (@{username})")
    
    # Проверяем лимит времени (кроме админа)
    if user_id != ADMIN_USER_ID:
        now = time.time()
        last_time = last_story_time.get(user_id)
        
        if last_time and now - last_time < LIMIT_SECONDS:
            hours_left = int((LIMIT_SECONDS - (now - last_time)) // 3600) + 1
            await message.answer(
                f"⏳ Ты уже отправлял историю недавно.\n"
                f"Приходи с новой историей через примерно {hours_left} ч."
            )
            return
    
    # Сохраняем сообщение в буфер
    msg_data = {
        'text': message.caption or message.text or '',
        'photo': message.photo[-1].file_id if message.photo else None,
        'timestamp': time.time()
    }
    
    user_messages[user_id].append(msg_data)
    
    # Ждем 2 секунды, не пришло ли еще сообщение от того же пользователя
    # (Telegram часто разбивает длинные сообщения с небольшой задержкой)
    await asyncio.sleep(2)
    
    # Проверяем, не было ли новых сообщений за это время
    # Если не было - отправляем на модерацию
    current_count = len(user_messages.get(user_id, []))
    
    # Отправляем в модерацию
    if MOD_CHAT_ID:
        await send_to_moderation(user_id, username)
    
    # Обновляем время последней истории
    if user_id != ADMIN_USER_ID:
        last_story_time[user_id] = time.time()
    
    # Уведомляем пользователя
    await message.answer("✅ История отправлена на модерацию")


# ---------- ОБРАБОТЧИКИ МОДЕРАЦИИ ----------

@router.callback_query(F.data.startswith("publish:"))
async def cb_publish(call: CallbackQuery):
    """Одобрение и публикация истории"""
    await call.answer("✅ Публикую...")
    
    try:
        user_id = int(call.data.split(":")[1])
    except:
        return
    
    print(f"✅ Одобрена история от user_id={user_id}")
    
    # Публикуем в канал
    message_ids = await publish_to_channel(user_id)
    
    if message_ids:
        # Помечаем в модерации
        current_text = call.message.text or ""
        new_text = current_text + "\n\n✅ <b>Опубликовано</b>"
        
        try:
            await call.message.edit_text(new_text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        
        await call.message.answer(f"✅ История от user_id={user_id} опубликована!")
    else:
        await call.message.answer(f"❌ Не удалось опубликовать историю")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    """Отклонение истории"""
    await call.answer("❌ Отклонено")
    
    try:
        user_id = int(call.data.split(":")[1])
    except:
        return
    
    print(f"❌ Отклонена история от user_id={user_id}")
    
    # Очищаем буфер пользователя
    if user_id in user_messages:
        del user_messages[user_id]
    
    # Помечаем в модерации
    current_text = call.message.text or ""
    new_text = current_text + "\n\n❌ <b>Отклонено</b>"
    
    try:
        await call.message.edit_text(new_text, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    
    # Уведомляем автора
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "Твоя история не прошла модерацию.\n\n"
                "Проверь, пожалуйста, чтобы не было:\n"
                "• политики и споров о власти\n"
                "• брани и грубых выражений\n"
                "• оскорблений и насмешек\n"
                "• пропаганды насилия\n\n"
                "Попробуй пересказать историю чуть мягче."
            ),
        )
    except Exception:
        pass


# ---------- ЗАПУСК ----------

async def main():
    print("🤖 БОТ ЗАПУЩЕН")
    print("✅ ГОТОВ К РАБОТЕ!")
    print("=" * 50)
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    asyncio.run(main())
