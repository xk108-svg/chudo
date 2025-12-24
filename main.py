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


# 🔥 ГЛОБАЛЬНЫЙ БУФЕР ДЛЯ СОХРАНЕНИЯ ЧАСТЕЙ ИСТОРИЙ ПО USER_ID
user_stories: Dict[int, List[Dict]] = {}
USER_BUFFER_SIZE = 3  # максимум 3 части от одного пользователя


# ---------- НАСТРОЙКИ ПРОЕКТА ----------

ADMIN_USER_ID = 318289611  # твой Telegram ID
LIMIT_SECONDS = 2 * 24 * 60 * 60  # 2 дня

# user_id -> timestamp последней отправленной истории
last_story_ts: Dict[int, float] = {}


# ---------- МОДЕЛЬ ИСТОРИИ ----------

@dataclass
class Story:
    id: Optional[int]
    user_id: int
    username: str
    text: str
    status: str = "pending"
    type: str = "text"  # "text" или "photo"
    photo_file_id: Optional[str] = None


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


async def save_story_to_supabase(story: Story) -> Optional[int]:
    """Сохраняет историю, возвращает ID записи или None."""
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


# ---------- СЛУЖЕБНЫЕ ФУНКЦИИ ----------

def moderation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Кнопки модерации с user_id вместо story_id"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"approve:{user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{user_id}",
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
    """
    ✅ РЕКЛАМА БЕЗ ЛИШНЕЙ КНОПКИ + КАРТИНКА ОДНИМ ПОСТОМ
    """
    # Reply на фото для рекламы с картинкой
    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1]
        ad_text = message.text[4:].strip()
        
        if not ad_text:
            await message.answer("❌ После /ad напиши текст объявления.")
            return
            
        # ✅ РЕКЛАМА С КАРТИНКОЙ - ТОЛЬКО "Поделись историей"
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
        
        # Чистка следов
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

    # Текстовая реклама - ТОЛЬКО "Поделись историей"
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


# 🔥 ОСНОВНОЙ ХЕНДЛЕР + БУФЕР ПО USER_ID
@router.message(
    (F.photo & ~F.reply_to_message) | 
    (F.text & ~F.text.startswith(("/ad", "/start")))
)
async def handle_story(message: Message):
    """
    ✅ ЛОВИТ ВСЁ ОТ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ:
    * Фото без текста
    * Фото + текст  
    * Просто текст (короткий/длинный)
    
    ✅ СОХРАНЯЕТ ПОСЛЕДНИЕ 3 СООБЩЕНИЯ ОТ КАЖДОГО ПОЛЬЗОВАТЕЛЯ ДЛЯ СКЛЕЙКИ
    """
    print(f"📨 ЛОВИМ: {message.from_user.id} | len={len(message.text or message.caption or '')}")
    
    user = message.from_user
    user_id = user.id

    # Ограничение ТОЛЬКО для пользователей (админ без лимита)
    if user.id != ADMIN_USER_ID:
        now = time.time()
        last_ts = last_story_ts.get(user_id)
        if last_ts and now - last_ts < LIMIT_SECONDS:
            hours_left = int((LIMIT_SECONDS - (now - last_ts)) // 3600) + 1
            await message.answer(
                f"⏳ Ты уже делился историей недавно.\n"
                f"Пожалуйста, приходи с новой историей через примерно {hours_left} ч."
            )
            return
        last_story_ts[user_id] = now

    # 🔥 БУФЕР ПО USER_ID — СОХРАНЯЕМ ВСЕ ПОСЛЕДНИЕ СООБЩЕНИЯ
    text = message.caption or message.text or ""
    has_photo = message.photo is not None
    photo_file_id = message.photo[-1].file_id if has_photo else None
    
    if user_id not in user_stories:
        user_stories[user_id] = []
    
    # Добавляем в буфер
    user_stories[user_id].append({
        'text': text,
        'photo': photo_file_id,
        'username': user.username or "anon",
        'timestamp': time.time()
    })
    
    # Ограничиваем размер буфера (последние 3 сообщения)
    if len(user_stories[user_id]) > USER_BUFFER_SIZE:
        user_stories[user_id] = user_stories[user_id][-USER_BUFFER_SIZE:]
    
    print(f"🔗 User {user_id}: {len(user_stories[user_id])} частей в буфере")

    # Сохраняем в Supabase
    story_type = "photo" if has_photo else "text"
    story = Story(
        id=None,
        user_id=user_id,
        username=user.username or "anon",
        text=text,
        type=story_type,
        photo_file_id=photo_file_id,
    )
    story_id = await save_story_to_supabase(story)

    await message.answer("История отправлена на модерацию ✅")

    # Шлём модераторам
    if MOD_CHAT_ID:
        content_type = "📷 Только фото" if has_photo and not text else \
                      "📷 Фото + текст" if has_photo else "📝 Текст"
        parts_count = len(user_stories[user_id])
        
        header = (
            f"🆕 Новая история\n"
            f"Тип: {content_type}\n"
            f"Автор: @{story.username} (id {user_id})\n"
            f"🔗 Части в буфере: {parts_count}\n"
            f"ID БД: {story_id or 'нет'}\n\n"
        )

        kb = moderation_keyboard(user_id)  # 🔥 user_id вместо story_id!

        try:
            if has_photo:
                await bot.send_photo(
                    MOD_CHAT_ID,
                    photo=photo_file_id,
                    caption=header + text,
                    reply_markup=kb,
                )
                print(f"✅ ОТПРАВЛЕНО В МОД: фото + {len(text)} симв. (user_id={user_id})")
            else:
                await bot.send_message(
                    MOD_CHAT_ID,
                    header + text,
                    reply_markup=kb,
                )
                print(f"✅ ОТПРАВЛЕНО В МОД: текст {len(text)} симв. (user_id={user_id})")
        except Exception as e:
            print(f"❌ ОШИБКА ОТПРАВКИ В МОД: {e}")


# 🔥 МОДЕРАЦИЯ СО СКЛЕЙКОЙ ПО USER_ID
@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery):
    await call.answer()

    # 🔥 Парсим user_id из callback_data
    payload = call.data.split(":", 1)[1]
    try:
        user_id = int(payload)
    except ValueError:
        await call.message.answer("Ошибка: некорректный ID пользователя.")
        return

    print(f"✅ ОДОБРЯЕМ user_id={user_id}")
    
    # 🔥 СКЛЕИВАЕМ ВСЕ ЧАСТИ ПО ЭТОМУ USER_ID!
    full_text = ""
    photo_file_id = None
    
    if user_id in user_stories and user_stories[user_id]:
        parts = user_stories[user_id]
        print(f"🔗 НАЙДЕНО {len(parts)} ЧАСТЕЙ ОТ user_id={user_id}")
        
        # Собираем ВСЕ тексты и последнее фото
        for part in parts:
            if part['photo']:
                photo_file_id = part['photo']  # Берём последнее фото
            if part['text']:
                full_text += part['text'] + "\n\n"
        
        full_text = full_text.strip()
        print(f"📝 СКЛЕЕННЫЙ ТЕКСТ: {len(full_text)} символов")
        
        # 🔥 Очищаем буфер после склейки
        del user_stories[user_id]
        print(f"🗑️ БУФЕР ОЧИЩЕН для user_id={user_id}")
    else:
        # Одиночное сообщение
        full_text = call.message.caption or call.message.text or ""
        lines = full_text.split("\n")
        full_text = "\n".join(lines[4:]).strip() if len(lines) > 4 else full_text
        photo_file_id = call.message.photo[-1].file_id if call.message.photo else None
        print(f"📝 ОДИНОЧНОЕ: {len(full_text)} символов")

    # ПУБЛИКАЦИЯ СКЛЕЕННЫМ КОНТЕНТОМ
    try:
        kb = share_your_story_keyboard()
        if photo_file_id:
            await bot.send_photo(
                CHANNEL_ID,
                photo=photo_file_id,
                caption=full_text or None,
                reply_markup=kb,
            )
            print("✅ ОПУБЛИКОВАНО: ФОТО + ТЕКСТ")
        elif full_text:
            await bot.send_message(
                CHANNEL_ID,
                full_text,
                reply_markup=kb,
            )
            print("✅ ОПУБЛИКОВАНО: ТЕКСТ")
        else:
            await bot.send_message(
                CHANNEL_ID,
                " ",
                reply_markup=kb,
            )
            print("✅ ОПУБЛИКОВАНО: ПУСТОЕ")
    except Exception as e:
        print(f"❌ ОШИБКА ПУБЛИКАЦИИ: {e}")
        await call.message.answer(f"❌ Ошибка публикации: {e}")
        return

    # Удаляем из Supabase (последнюю запись)
    if SUPABASE_ENABLED:
        # Удаляем все записи этого пользователя за последние 5 минут
        five_min_ago = int(time.time() - 300)
        params = {"user_id": f"eq.{user_id}", "created_at": f"gte.{five_min_ago}"}
        await supabase_request("DELETE", "/rest/v1/stories", params=params)

    # Уведомляем автора
    full_text_for_user_id = call.message.caption or call.message.text or ""
    extracted_user_id = extract_user_id_from_moderation_text(full_text_for_user_id)
    if extracted_user_id:
        try:
            await bot.send_message(
                chat_id=extracted_user_id,
                text="✨ Твоя история прошла модерацию и опубликована в канале. Спасибо, что делишься чудом!",
            )
            print(f"✅ УВЕДОМЛЁН АВТОР: {extracted_user_id}")
        except Exception as e:
            print("Cannot notify user:", e)

    # Помечаем сообщение модерации
    current_text = call.message.caption or call.message.text or ""
    new_text = current_text + "\n\n✅ <b>Одобрено и опубликовано!</b>"
    try:
        if call.message.photo:
            await call.message.edit_caption(new_text)
        else:
            await call.message.edit_text(new_text)
    except Exception as e:
        print(f"Ошибка редактирования модерации: {e}")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    await call.answer()

    payload = call.data.split(":", 1)[1]
    try:
        user_id = int(payload)
    except ValueError:
        user_id = 0

    # Очищаем буфер пользователя
    if user_id in user_stories:
        del user_stories[user_id]
        print(f"🗑️ БУФЕР ОЧИЩЕН (отклонено) для user_id={user_id}")

    full_text = call.message.caption or call.message.text or ""

    # Уведомляем автора
    extracted_user_id = extract_user_id_from_moderation_text(full_text)
    if extracted_user_id:
        try:
            await bot.send_message(
                chat_id=extracted_user_id,
                text=(
                    "Твоя история не прошла модерацию и не была опубликована.\n\n"
                    "Проверь, пожалуйста, чтобы не было политики, брани и оскорблений, "
                    "и попробуй пересказать её чуть мягче."
                ),
            )
        except Exception as e:
            print("Cannot notify user:", e)

    # Помечаем сообщение модерации
    current_text = call.message.caption or call.message.text or ""
    new_text = current_text + "\n\n❌ <b>Отклонено</b>"
    try:
        if call.message.photo:
            await call.message.edit_caption(new_text)
        else:
            await call.message.edit_text(new_text)
    except Exception as e:
        print(f"Ошибка редактирования модерации: {e}")


# ---------- ЗАПУСК ----------

async def main():
    print("🤖 Bot started polling...")
    print(f"📺 Канал ID: {CHANNEL_ID}")
    print(f"🛡️ Модерация ID: {MOD_CHAT_ID or 'НЕ ЗАДАН'}")
    print(f"🔗 Склейка по USER_ID: ВКЛЮЧЕНА (макс {USER_BUFFER_SIZE} частей)")
    print("✅ ГОТОВ К РАБОТЕ!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
