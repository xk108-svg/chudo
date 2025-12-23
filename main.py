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

ADMIN_USER_ID = 318289611  # твой Telegram ID
LIMIT_SECONDS = 2 * 24 * 60 * 60  # 2 дня

# user_id -> timestamp последней отправленной истории
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
    type: str = "text"  # "text", "photo", "long_story"
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
    "Хештеги ставь в самом низу сообщения, слитно, без пробелов внутри.\n\n"
    "<b>Команды:</b>\n"
    "/story — короткая история (до 4000 символов)\n"
    "/long_story — длинная история (до 30 000 символов)\n"
    "/ad — реклама"
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


# ---------- ✅ ДЛИННАЯ ИСТОРИЯ — ОТДЕЛЬНЫЕ ХЕНДЛЕРЫ ----------

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
    print(f"PART1: {message.from_user.id} - {len(message.text)} символов")
    if len(message.text) > 4000:
        await message.answer("❌ Часть до 4000 символов! Попробуй ещё раз:")
        return
    await state.update_data(part1=message.text)
    await message.answer("✍️ <b>Часть 2/3</b> (до 4000 символов):")
    await state.set_state(LongStory.part2)


@router.message(LongStory.part2)
async def long_part2(message: Message, state: FSMContext):
    print(f"PART2: {message.from_user.id} - {len(message.text)} символов")
    if len(message.text) > 4000:
        await message.answer("❌ Часть до 4000 символов! Попробуй ещё раз:")
        return
    await state.update_data(part2=message.text)
    await message.answer("✍️ <b>Часть 3/3</b> (до 4000 символов):")
    await state.set_state(LongStory.part3)


@router.message(LongStory.part3)
async def long_part3(message: Message, state: FSMContext):
    print(f"PART3: {message.from_user.id} - {len(message.text)} символов")
    if len(message.text) > 4000:
        await message.answer("❌ Часть до 4000 символов! Попробуй ещё раз:")
        return
    await state.update_data(part3=message.text)
    await message.answer("📷 Отправь фото (или напиши 'без фото'):")
    await state.set_state(LongStory.photo)


@router.message(LongStory.photo)
async def long_photo(message: Message, state: FSMContext):
    print(f"PHOTO: {message.from_user.id} - Photo: {bool(message.photo)}, Text: '{message.text}'")
    
    data = await state.get_data()
    
    # Собираем ВСЮ историю
    full_story = (
        f"<b>{data['title']}</b>\n\n"
        f"<b>Часть 1:</b>\n{data['part1']}\n\n"
        f"<b>Часть 2:</b>\n{data['part2']}\n\n"
        f"<b>Часть 3:</b>\n{data['part3']}"
    )
    
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    elif message.text and "без фото" in message.text.lower():
        pass
    else:
        await message.answer("❌ Отправь фото или напиши 'без фото':")
        return

    # ✅ СОХРАНЯЕМ И ОТПРАВЛЯЕМ В МОДЕРАЦИЮ
    story = Story(
        user_id=data['user_id'],
        username=data['username'],
        text=full_story,
        type="long_story",
        photo_file_id=photo_file_id,
    )
    
    story_id = await save_story_to_supabase(story)
    await state.clear()
    
    await message.answer("✅ Длинная история отправлена на модерацию!")
    print(f"✅ FULL STORY SAVED: {data['user_id']}, {len(full_story)} chars")

    # ОТПРАВЛЯЕМ В МОДЕРАЦИЮ
    if MOD_CHAT_ID:
        supabase_mark = f"ID в БД: {story_id}" if story_id else "⚠️ Ошибка БД"
        header = (
            f"🆕 <b>ДЛИННАЯ ИСТОРИЯ</b>\n"
            f"Автор: @{data['username']} (id {data['user_id']})\n"
            f"📄 {len(full_story)} символов\n"
            f"{'📷 + фото' if photo_file_id else '📝 только текст'}\n"
            f"{supabase_mark}\n\n"
        )
        kb = moderation_keyboard(story_id or 0)

        try:
            if photo_file_id:
                await bot.send_photo(
                    MOD_CHAT_ID, 
                    photo=photo_file_id, 
                    caption=header + full_story, 
                    reply_markup=kb
                )
            else:
                await bot.send_message(
                    MOD_CHAT_ID, 
                    header + full_story, 
                    reply_markup=kb
                )
            print("✅ SENT TO MODERATION!")
        except Exception as e:
            print(f"❌ MODERATION ERROR: {e}")


# ---------- ✅ КОРОТКАЯ ИСТОРИЯ ----------

@router.message(
    (F.photo & ~F.reply_to_message) | 
    (F.text & ~F.text.startswith(("/ad", "/start", "/long_story", "/cancel")))
)
async def handle_short_story(message: Message):
    user = message.from_user

    if user.id != ADMIN_USER_ID:
        now = time.time()
        last_ts = last_story_ts.get(user.id)
        if last_ts and now - last_ts < LIMIT_SECONDS:
            hours_left = int((LIMIT_SECONDS - (now - last_ts)) // 3600) + 1
            await message.answer(
                "Ты уже делился историей недавно.\n"
                f"Пожалуйста, приходи с новой историей через примерно {hours_left} ч."
            )
            return
        last_story_ts[user.id] = now

    has_photo = message.photo is not None
    text = message.caption or message.text or ""
    story_type = "photo" if has_photo else "text"
    photo_file_id = message.photo[-1].file_id if has_photo else None

    story = Story(
        id=None,
        user_id=user.id,
        username=user.username or "anon",
        text=text,
        type=story_type,
        photo_file_id=photo_file_id,
    )

    story_id = await save_story_to_supabase(story)
    await message.answer("История отправлена на модерацию ✅")

    if MOD_CHAT_ID:
        supabase_mark = f"ID в БД: {story_id}" if story_id else "⚠️ Ошибка БД"
        content_type = "📷 Только фото" if has_photo and not text.strip() else \
                      "📷 Фото + текст" if has_photo else "📝 Текст"
        
        header = (
            f"🆕 Короткая история\n"
            f"Тип: {content_type}\n"
            f"Автор: @{story.username} (id {story.user_id})\n"
            f"{supabase_mark}\n\n"
        )

        kb = moderation_keyboard(story_id or 0)

        if story_type == "photo":
            await bot.send_photo(
                MOD_CHAT_ID,
                photo=photo_file_id,
                caption=header + text,
                reply_markup=kb,
            )
        else:
            await bot.send_message(
                MOD_CHAT_ID,
                header + text,
                reply_markup=kb,
            )


# ---------- ХЕНДЛЕРЫ МОДЕРАЦИИ ----------

@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery):
    await call.answer()

    payload = call.data.split(":", 1)[1]
    try:
        story_id = int(payload)
    except ValueError:
        await call.message.answer("Ошибка: некорректный ID истории.")
        return

    full_text = call.message.caption or call.message.text or ""
    
    lines = full_text.split("\n")
    story_text = "\n".join(lines[4:]).strip() if len(lines) > 4 else ""
    
    if "ДЛИННАЯ ИСТОРИЯ" in full_text:
        story_text = story_text[:4000] + "\n\n🔗 <b>Полная история в Дзене:</b> dzen.ru/your_channel"
    
    if not story_text.strip():
        story_text = None

    if call.message.photo:
        photo = call.message.photo[-1]
        await bot.send_photo(
            CHANNEL_ID,
            photo=photo.file_id,
            caption=story_text,
            reply_markup=share_your_story_keyboard(),
        )
    else:
        await bot.send_message(
            CHANNEL_ID,
            story_text or " ",
            reply_markup=share_your_story_keyboard(),
        )

    if story_id != 0:
        deleted = await delete_story_from_supabase(story_id)
        print("Supabase delete:", deleted)

    user_id = extract_user_id_from_moderation_text(full_text)
    if user_id:
        story_type = "длинная история" if "ДЛИННАЯ ИСТОРИЯ" in full_text else "история"
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"✨ Твоя {story_type} прошла модерацию и опубликована в канале. Спасибо!",
            )
        except Exception as e:
            print("Cannot notify user:", e)

    suffix = "\n\n✅ Одобрено и опубликовано."
    if not full_text.endswith("✅ Одобрено и опубликовано."):
        new_text = full_text + suffix
        if call.message.photo:
            await call.message.edit_caption(new_text)
        else:
            await call.message.edit_text(new_text)


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    await call.answer()

    payload = call.data.split(":", 1)[1]
    try:
        story_id = int(payload)
    except ValueError:
        story_id = 0

    if story_id != 0:
        deleted = await delete_story_from_supabase(story_id)
        print("Supabase delete (reject):", deleted)

    full_text = call.message.caption or call.message.text or ""
    user_id = extract_user_id_from_moderation_text(full_text)
    if user_id:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "Твоя история не прошла модерацию.\n\n"
                    "Проверь, пожалуйста, чтобы не было политики, брани и оскорблений, "
                    "и попробуй пересказать её мягче."
                ),
            )
        except Exception as e:
            print("Cannot notify user:", e)

    suffix = "\n\n❌ Отклонено."
    if not full_text.endswith("❌ Отклонено."):
        new_text = full_text + suffix
        if call.message.photo:
            await call.message.edit_caption(new_text)
        else:
            await call.message.edit_text(new_text)


# ---------- ЗАПУСК ----------

async def main():
    print("Bot started polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
