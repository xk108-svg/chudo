import asyncio
import os
import time
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


# 🔥 СТРУКТУРА ДЛЯ ИСТОРИЙ
class UserStory:
    def __init__(self, user_id: int, username: str):
        self.user_id = user_id
        self.username = username
        self.messages: List[Dict] = []  # список сообщений
        self.moderation_msg_id: Optional[int] = None  # ID кнопок в модерации
        self.timestamp = time.time()
        self.task: Optional[asyncio.Task] = None  # задача отправки на модерацию

user_stories: Dict[int, UserStory] = {}  # user_id -> UserStory
last_story_time: Dict[int, float] = {}  # user_id -> время последней истории


# ---------- НАСТРОЙКИ ----------

ADMIN_USER_ID = 318289611
LIMIT_SECONDS = 2 * 24 * 60 * 60


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
    """Кнопки для модерации (в конце блока пользователя)"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"publish:{user_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
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


async def send_story_to_moderation(user_id: int):
    """Отправляет историю пользователя в модерацию одним блоком"""
    if not MOD_CHAT_ID or user_id not in user_stories:
        return
    
    story = user_stories[user_id]
    
    if len(story.messages) == 0:
        return
    
    print(f"📤 Отправка в модерацию: {len(story.messages)} сообщений от user_id={user_id}")
    
    try:
        # Отправляем ВСЕ сообщения пользователя подряд
        for msg_data in story.messages:
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
                await asyncio.sleep(0.05)
            except Exception as e:
                print(f"⚠️ Ошибка отправки сообщения: {e}")
        
        # В КОНЦЕ блока отправляем кнопки модерации
        footer_msg = await bot.send_message(
            MOD_CHAT_ID,
            f"┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅\n"
            f"📨 История от @{story.username} (id {user_id})\n"
            f"📊 Сообщений: {len(story.messages)}\n"
            f"🕐 Время: {datetime.fromtimestamp(story.timestamp).strftime('%H:%M:%S')}",
            reply_markup=moderation_keyboard(user_id)
        )
        
        story.moderation_msg_id = footer_msg.message_id
        
        print(f"✅ История user_id={user_id} отправлена в модерацию")
        
        # Отправляем уведомление пользователю
        try:
            await bot.send_message(
                chat_id=user_id,
                text="✅ История отправлена на модерацию. Ожидайте публикации в канале.",
            )
        except Exception:
            pass
        
    except Exception as e:
        print(f"❌ Ошибка отправки истории в модерацию: {e}")


async def publish_to_channel(user_id: int):
    """Публикует историю в канал"""
    if user_id not in user_stories:
        return []
    
    story = user_stories[user_id]
    published_ids = []
    
    print(f"🚀 Публикация {len(story.messages)} сообщений от user_id={user_id}")
    
    # Публикуем ВСЕ сообщения
    for i, msg_data in enumerate(story.messages):
        try:
            is_last = (i == len(story.messages) - 1)
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
            await asyncio.sleep(0.05)
            
        except Exception as e:
            print(f"❌ Ошибка публикации: {e}")
    
    # Удаляем историю из памяти
    if user_id in user_stories:
        del user_stories[user_id]
    
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
    """Принимает сообщения от пользователей"""
    user = message.from_user
    user_id = user.id
    username = user.username or "anon"
    
    print(f"📨 Сообщение от {user_id} (@{username})")
    
    # ⚠️ ИСПРАВЛЕНО: Проверяем лимит времени (кроме админа)
    # Но сбрасываем счетчик, если прошло достаточно времени
    now = time.time()
    
    if user_id != ADMIN_USER_ID:
        last_time = last_story_time.get(user_id)
        
        # Если прошло больше 1 часа с последней истории, сбрасываем счетчик
        if last_time and now - last_time > 3600:  # 1 час
            print(f"🔄 Сброс счетчика для user_id={user_id} (прошло >1 часа)")
            last_story_time[user_id] = now
            # Также очищаем историю, если она есть
            if user_id in user_stories:
                del user_stories[user_id]
        
        # Проверяем стандартный лимит (2 дня)
        elif last_time and now - last_time < LIMIT_SECONDS:
            hours_left = int((LIMIT_SECONDS - (now - last_time)) // 3600) + 1
            await message.answer(
                f"⏳ Ты уже отправлял историю недавно.\n"
                f"Приходи с новой историей через примерно {hours_left} ч."
            )
            return
    
    # ⚠️ ИСПРАВЛЕНО: ВСЕГДА создаем новую историю при первом сообщении
    # или если прошло больше 5 минут с последнего сообщения
    should_create_new = True
    
    if user_id in user_stories:
        story = user_stories[user_id]
        time_since_last = now - story.timestamp
        
        # Если прошло меньше 5 минут, продолжаем текущую историю
        if time_since_last < 300:  # 5 минут
            should_create_new = False
        else:
            # Прошло больше 5 минут - создаем новую историю
            print(f"🆕 Новая история для user_id={user_id} (прошло >5 минут)")
            # Старую историю отбрасываем
            del user_stories[user_id]
    
    if should_create_new:
        user_stories[user_id] = UserStory(user_id, username)
        print(f"🆕 Создана новая история для user_id={user_id}")
    
    story = user_stories[user_id]
    
    # Добавляем сообщение в историю
    msg_data = {
        'text': message.caption or message.text or '',
        'photo': message.photo[-1].file_id if message.photo else None,
        'timestamp': time.time()
    }
    
    story.messages.append(msg_data)
    story.timestamp = now
    
    # ⚠️ ИСПРАВЛЕНО: УБИРАЕМ ЛИШНИЕ УВЕДОМЛЕНИЯ
    # Не отправляем "Сообщение X принято" - это лишнее
    
    # Отменяем предыдущую задачу отправки, если она есть
    if story.task and not story.task.done():
        story.task.cancel()
        print(f"🔄 Отменена предыдущая задача отправки для user_id={user_id}")
    
    # Запускаем новую задачу отправки на модерацию через 3 секунды
    async def send_after_delay():
        await asyncio.sleep(3)  # Ждем 3 секунды
        if user_id in user_stories and user_stories[user_id] is story:
            await send_story_to_moderation(user_id)
            # Обновляем время последней истории
            if user_id != ADMIN_USER_ID:
                last_story_time[user_id] = time.time()
    
    story.task = asyncio.create_task(send_after_delay())
    print(f"⏰ Запланирована отправка истории user_id={user_id} через 3 секунды")


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
        new_text = current_text + "\n\n✅ <b>ОПУБЛИКОВАНО</b>"
        
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
    
    # Удаляем историю из памяти
    if user_id in user_stories:
        del user_stories[user_id]
    
    # Помечаем в модерации
    current_text = call.message.text or ""
    new_text = current_text + "\n\n❌ <b>ОТКЛОНЕНО</b>"
    
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
    print("=" * 50)
    print("📝 ЛОГИКА РАБОТЫ:")
    print("1. Пользователь пишет сообщения")
    print("2. После 3 секунд тишины - отправка в модерацию")
    print("3. НЕТ лишних сообщений 'Сообщение X принято'")
    print("4. Счетчик сбрасывается через 1 час неактивности")
    print("5. Новая история через 5 минут тишины")
    print("=" * 50)
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    asyncio.run(main())
