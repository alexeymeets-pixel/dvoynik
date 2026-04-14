import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI, APIError
from db import (init_db, get_user, get_user_by_username, save_user_field,
                get_onboarding_step, set_onboarding_step, save_message,
                get_history, get_friends, get_pending_requests,
                send_friend_request, accept_friend_request)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

ONBOARDING_STEPS = [
    ("name",         "Привет! Я твой личный агент-двойник 👤\n\nКак тебя зовут?", None),
    ("agent_name",   "Отлично! Придумай имя своему агенту — он будет представляться этим именем в сети:", None),
    ("age_range",    "Сколько тебе лет?", ["До 25", "25–35", "35–45", "45+"]),
    ("city",         "В каком городе живёшь?", None),
    ("occupation",   "Чем занимаешься?", ["Предприниматель", "Фрилансер", "Работаю в компании", "Студент", "Другое"]),
    ("sphere",       "Напиши свою сферу — кино, IT, дизайн, маркетинг и т.д.:", None),
    ("goal",         "Что сейчас важнее всего?", ["Найти партнёра или команду", "Найти клиентов", "Найти работу", "Развиваться и учиться", "Просто общаться"]),
    ("open_to_meet", "Открыт к новым знакомствам?", ["Да, всегда", "Только по делу", "Пока нет"]),
    ("free_time",    "Как проводишь свободное время? Напиши пару слов:", None),
    ("proud_of",     "Чем гордишься или что делаешь лучше всего?", None),
]

SYSTEM_PROMPT = """Ты личный AI-агент по имени {agent_name} — двойник пользователя по имени {name}.

Что ты знаешь о пользователе:
- Имя: {name}
- Город: {city}
- Сфера: {sphere}
- Занятие: {occupation}
- Цель сейчас: {goal}
- Открыт к знакомствам: {open_to_meet}
- Свободное время: {free_time}
- Гордится: {proud_of}

Друзья пользователя в сети:
{friends_info}

Твоя задача:
- Общаться как живой человек — тепло, естественно, без формальностей
- Задавать один вопрос за раз чтобы лучше узнать пользователя
- Помогать находить нужных людей среди друзей
- Если пользователь хочет встречу — предложи время и организуй

Говори только по-русски. Ответы короткие — 2-3 предложения максимум.
Никогда не показывай полный список друзей — используй эти знания тихо в фоне."""


def build_keyboard(options):
    if not options:
        return ReplyKeyboardRemove()
    buttons = [[KeyboardButton(text=opt)] for opt in options]
    return ReplyKeyboardMarkup(keyboard=buttons, one_time_keyboard=True, resize_keyboard=True)


def build_inline(buttons):
    kb = [[InlineKeyboardButton(text=b[0], callback_data=b[1])] for b in buttons]
    return InlineKeyboardMarkup(inline_keyboard=kb)


@dp.message(CommandStart())
async def start(message: types.Message):
    telegram_id = message.from_user.id
    username = message.from_user.username or ""
    
    # Сохраняем username
    save_user_field(telegram_id, "username", username)
    
    user = get_user(telegram_id)

    # Проверяем входящие запросы в друзья
    pending = get_pending_requests(telegram_id)
    if pending:
        for req in pending:
            name = req["name"] or "Кто-то"
            agent = req["agent_name"] or "Агент"
            kb = build_inline([
                [f"✅ Принять {name}", f"accept_{req['telegram_id']}"],
                [f"❌ Отклонить", f"decline_{req['telegram_id']}"]
            ])
            await message.answer(
                f"👋 {name} хочет добавить тебя в друзья.\nЕго агент: {agent}",
                reply_markup=kb
            )

    if user and user["onboarding_done"]:
        agent_name = user["agent_name"] or "Двойник"
        friends = get_friends(telegram_id)
        friends_count = len(friends)
        await message.answer(
            f"С возвращением! Я {agent_name}.\n"
            f"У тебя {friends_count} друзей в сети.\n\n"
            f"Чем могу помочь?",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    set_onboarding_step(telegram_id, 0)
    step_data = ONBOARDING_STEPS[0]
    await message.answer(step_data[1], reply_markup=build_keyboard(step_data[2]))


@dp.callback_query(F.data.startswith("accept_"))
async def accept_friend(callback: types.CallbackQuery):
    to_id = callback.from_user.id
    from_id = int(callback.data.split("_")[1])
    
    accept_friend_request(from_id, to_id)
    
    from_user = get_user(from_id)
    name = from_user["name"] if from_user else "Пользователь"
    
    await callback.message.edit_text(f"✅ Вы теперь друзья с {name}!")
    
    # Уведомляем отправителя
    try:
        my_user = get_user(to_id)
        my_name = my_user["name"] if my_user else "Кто-то"
        await bot.send_message(from_id, f"🎉 {my_name} принял твой запрос в друзья!")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("decline_"))
async def decline_friend(callback: types.CallbackQuery):
    await callback.message.edit_text("❌ Запрос отклонён.")



@dp.message(F.voice)
async def handle_voice(message: types.Message):
    telegram_id = message.from_user.id
    try:
        file = await bot.get_file(message.voice.file_id)
        file_bytes = await bot.download_file(file.file_path)
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", file_bytes, "audio/ogg"),
            language="ru"
        )
        text = transcript.text.strip()
        if not text:
            await message.answer("Не смог разобрать голосовое — попробуй ещё раз.")
            return
        await message.answer(f"🎤 _{text}_", parse_mode="Markdown")
        await process_agent(message, telegram_id, text)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await message.answer("Не смог обработать голосовое. Попробуй текстом.")

@dp.message(F.text)
async def handle_message(message: types.Message):
    telegram_id = message.from_user.id
    text = message.text.strip()
    username = message.from_user.username or ""

    if not text:
        return

    # Сохраняем username при каждом сообщении
    save_user_field(telegram_id, "username", username)

    user = get_user(telegram_id)

    # Онбординг
    if not user or not user["onboarding_done"]:
        step = get_onboarding_step(telegram_id)

        if step < len(ONBOARDING_STEPS):
            field, _, _ = ONBOARDING_STEPS[step]
            save_user_field(telegram_id, field, text)
            next_step = step + 1

            if next_step < len(ONBOARDING_STEPS):
                set_onboarding_step(telegram_id, next_step)
                next_data = ONBOARDING_STEPS[next_step]
                await message.answer(next_data[1], reply_markup=build_keyboard(next_data[2]))
            else:
                save_user_field(telegram_id, "onboarding_done", 1)
                user = get_user(telegram_id)
                agent_name = user["agent_name"] or "Двойник"
                await message.answer(
                    f"Отлично, {user['name']}! 🎉\n\n"
                    f"Я {agent_name} — твой личный агент. Буду рядом.\n\n"
                    f"Чтобы добавить друга напиши: *добавить @username*\n\n"
                    f"Как прошёл твой день?",
                    parse_mode="Markdown",
                    reply_markup=ReplyKeyboardRemove()
                )
        return

    # Команда добавить друга
    if text.lower().startswith("добавить") and "@" in text:
        parts = text.split("@")
        if len(parts) > 1:
            target_username = parts[1].strip().split()[0]
            target = get_user_by_username(target_username)
            
            if not target:
                await message.answer(f"Пользователь @{target_username} не найден в сети. Возможно он ещё не зарегистрировался.")
                return
            
            if target["telegram_id"] == telegram_id:
                await message.answer("Это твой собственный аккаунт 😄")
                return
            
            send_friend_request(telegram_id, target["telegram_id"])
            
            # Уведомляем целевого пользователя
            my_user = get_user(telegram_id)
            my_name = my_user["name"] or "Кто-то"
            my_agent = my_user["agent_name"] or "Агент"
            
            kb = build_inline([
                [f"✅ Принять", f"accept_{telegram_id}"],
                [f"❌ Отклонить", f"decline_{telegram_id}"]
            ])
            
            try:
                await bot.send_message(
                    target["telegram_id"],
                    f"👋 {my_name} хочет добавить тебя в друзья.\nЕго агент: {my_agent}",
                    reply_markup=kb
                )
            except Exception:
                pass
            
            target_name = target["name"] or target_username
            await message.answer(f"✅ Запрос отправлен {target_name}! Жду пока он примет.")
            return

    # Обычный разговор
    await process_agent(message, telegram_id, text)


async def process_agent(message: types.Message, telegram_id: int, text: str):
    user = get_user(telegram_id)
    history = get_history(telegram_id)
    friends = get_friends(telegram_id)

    if friends:
        friends_info = "\n".join([
            f"- {f['name']} ({f['sphere'] or 'сфера не указана'}, {f['city'] or 'город не указан'}), цель: {f['goal'] or 'не указана'}"
            for f in friends
        ])
    else:
        friends_info = "Друзей пока нет"

    system = SYSTEM_PROMPT.format(
        agent_name=user["agent_name"] or "Двойник",
        name=user["name"] or "друг",
        city=user["city"] or "не указан",
        sphere=user["sphere"] or "не указана",
        occupation=user["occupation"] or "не указано",
        goal=user["goal"] or "не указана",
        open_to_meet=user["open_to_meet"] or "не указано",
        free_time=user["free_time"] or "не указано",
        proud_of=user["proud_of"] or "не указано",
        friends_info=friends_info,
    )

    messages = history + [{"role": "user", "content": text}]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}] + messages,
            max_tokens=300,
            temperature=0.85,
        )
        reply = response.choices[0].message.content.strip()
    except APIError as e:
        logger.error(f"OpenAI API error: {e}")
        reply = "Что-то пошло не так, попробуй через минуту."

    save_message(telegram_id, "user", text)
    save_message(telegram_id, "assistant", reply)

    await message.answer(reply)


import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

async def main():
    init_db()
    # Запускаем HTTP сервер в отдельном потоке
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    logger.info(f"Health check server on port {port}")
    logger.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
