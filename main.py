import asyncio
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from supabase import create_client, Client

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MOD_CHAT_ID = int(os.getenv("MOD_CHAT_ID"))      # id чата модерации
CHANNEL_ID = os.getenv("CHANNEL_ID")            # @username или numeric id канала

bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- HELPERS ----------

def save_story_to_db(user_id: int, username: str | None, text: str, anon: bool, name: str | None):
    data = {
        "user_id": user_id,
        "username": username,
        "text": text,
        "is_anon": anon,
        "display_name": name,
        "status": "pending"
    }
    res = supabase.table("stories").insert(data).execute()
    return res.data[0]["id"]

def mark_story_status(story_id: int, status: str):
    supabase.table("stories").update({"status": status}).eq("id", story_id).execute()

def get_story(story_id: int):
    res = supabase.table("stories").select("*").eq("id", story_id).single().execute()
    return res.data

def delete_story(story_id: int):
    supabase.table("stories").delete().eq("id", story_id).execute()

# ---------- USER FLOW ----------

@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поделиться историей", callback_data="share_story")],
        [InlineKeyboardButton(text="Правила", callback_data="rules")]
    ])
    text = (
        "Привет! Это бот канала <b>«Делись Чудом»</b>.\n\n"
        "Здесь ты можешь анонимно или с именем поделиться своей историей "
        "исцеления, чудесного вмешательства Бога или духовного опыта.\n\n"
        "Нажми кнопку ниже, чтобы отправить историю."
    )
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "rules")
async def show_rules(call: CallbackQuery):
    text = (
        "<b>Правила:</b>\n\n"
        "• Пишем только личные истории чудес, исцелений, духовных переживаний.\n"
        "• Без оскорблений, политики, рекламы, матов.\n"
        "• Админы могут сокращать текст и отказывать в публикации без объяснения причин."
    )
    await call.message.edit_text(text)
    await call.answer()

# Простейшая FSM без хранения в БД: пользователь отправляет историю следующим сообщением
user_state = {}

@dp.callback_query(F.data == "share_story")
async def ask_story(call: CallbackQuery):
    user_state[call.from_user.id] = {"step": "await_story"}
    await call.message.edit_text(
        "Пожалуйста, отправь текст своей истории одним или несколькими сообщениями.\n\n"
        "Когда закончишь, напиши слово <b>ГОТОВО</b> отдельным сообщением."
    )
    await call.answer()

@dp.message()
async def catch_story(message: Message):
    uid = message.from_user.id
    state = user_state.get(uid)

    # если пользователь в процессе ввода истории
    if state and state.get("step") in ("await_story", "collect_story"):
        if message.text and message.text.strip().upper() == "ГОТОВО":
            text = state.get("text", "").strip()
            if not text:
                await message.answer("История пока пустая. Напиши текст и потом снова отправь слово ГОТОВО.")
                return
            # спрашиваем анонимность
            user_state[uid]["step"] = "ask_anon"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Анонимно", callback_data="anon_yes")],
                [InlineKeyboardButton(text="С именем", callback_data="anon_no")]
            ])
            await message.answer("Как опубликовать историю?", reply_markup=kb)
            return

        # накапливаем текст
        prev = state.get("text", "")
        new_text = (prev + "\n" + (message.text or "")).strip()
        user_state[uid]["text"] = new_text
        user_state[uid]["step"] = "collect_story"
        return

    # если нет активного сценария, игнорируем или даём подсказку
    if message.text == "/help":
        await message.answer("Нажми /start, чтобы поделиться историей.")
    # иначе можно ничего не делать

@dp.callback_query(F.data.in_(["anon_yes", "anon_no"]))
async def choose_anon(call: CallbackQuery):
    uid = call.from_user.id
    state = user_state.get(uid)
    if not state or state.get("step") != "ask_anon":
        await call.answer()
        return

    if call.data == "anon_yes":
        user_state[uid]["is_anon"] = True
        user_state[uid]["display_name"] = None
        await confirm_story(call.message, uid)
    else:
        user_state[uid]["is_anon"] = False
        user_state[uid]["step"] = "ask_name"
        await call.message.answer("Напиши, как подписать автора (имя или ник).")
    await call.answer()

@dp.message()
async def catch_name_or_default(message: Message):
    uid = message.from_user.id
    state = user_state.get(uid)
    if not state or state.get("step") != "ask_name":
        return
    user_state[uid]["display_name"] = message.text.strip()
    await confirm_story(message, uid)

async def confirm_story(message_or_msg, uid: int):
    state = user_state.get(uid)
    text = state.get("text", "").strip()
    is_anon = state.get("is_anon", True)
    display_name = state.get("display_name")

    author_line = "Автор: анонимно" if is_anon else f"Автор: {display_name}"
    preview = f"<b>Черновик истории:</b>\n\n{text}\n\n<i>{author_line}</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить на модерацию", callback_data="send_to_mod")],
        [InlineKeyboardButton(text="Отменить", callback_data="cancel_story")]
    ])

    await message_or_msg.answer(preview, reply_markup=kb)
    user_state[uid]["step"] = "confirm"

@dp.callback_query(F.data.in_(["send_to_mod", "cancel_story"]))
async def final_step(call: CallbackQuery):
    uid = call.from_user.id
    state = user_state.get(uid)
    if not state:
        await call.answer()
        return

    if call.data == "cancel_story":
        user_state.pop(uid, None)
        await call.message.edit_text("История отменена. Если захочешь, начни заново командой /start.")
        await call.answer()
        return

    if state.get("step") != "confirm":
        await call.answer()
        return

    text = state.get("text", "").strip()
    is_anon = state.get("is_anon", True)
    display_name = state.get("display_name")
    user = call.from_user

    story_id = save_story_to_db(
        user_id=user.id,
        username=user.username,
        text=text,
        anon=is_anon,
        name=display_name,
    )

    # отправляем в модераторский чат
    author_line = "Автор: анонимно" if is_anon else f"Автор: {display_name}"
    mod_text = (
        f"<b>Новая история #{story_id}</b>\n\n"
        f"{text}\n\n"
        f"<i>{author_line}</i>\n\n"
        f"tg_id: <code>{user.id}</code>, username: @{user.username or '—'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"mod_pub:{story_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod_rej:{story_id}")]
    ])
    await bot.send_message(chat_id=MOD_CHAT_ID, text=mod_text, reply_markup=kb)

    user_state.pop(uid, None)
    await call.message.edit_text("Спасибо! Твоя история отправлена на модерацию.")
    await call.answer()

# ---------- MODERATION CALLBACKS ----------

@dp.callback_query(F.data.startswith("mod_pub:"))
async def mod_publish(call: CallbackQuery):
    # проверка, что жмёт только модератор — по желанию:
    # if call.from_user.id not in ALLOWED_MODS: ...

    story_id = int(call.data.split(":")[1])
    story = get_story(story_id)
    if not story:
        await call.answer("История не найдена.", show_alert=True)
        return

    text = story["text"]
    if story["is_anon"]:
        author_line = "Автор: анонимно"
    else:
        author_line = f"Автор: {story['display_name'] or 'анонимно'}"

    post_text = f"{text}\n\n<i>{author_line}</i>"

    # публикуем в канал
    await bot.send_message(chat_id=CHANNEL_ID, text=post_text)

    # отмечаем как опубликовано и чистим историю
    mark_story_status(story_id, "published")
    delete_story(story_id)   # как ты просил — не множим базу

    with suppress(Exception):
        await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Опубликовано и удалено из базы.")

@dp.callback_query(F.data.startswith("mod_rej:"))
async def mod_reject(call: CallbackQuery):
    story_id = int(call.data.split(":")[1])
    mark_story_status(story_id, "rejected")
    delete_story(story_id)

    with suppress(Exception):
        await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("История отклонена и удалена из базы.")

# ---------- ENTRYPOINT ----------

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
