import os
import re
import logging
import asyncio
import threading
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI, APIError
from tavily import TavilyClient
from db import (init_db, get_user, get_user_by_username, save_user_field,
                get_onboarding_step, set_onboarding_step, save_message,
                get_history, get_friends, get_pending_requests,
                send_friend_request, accept_friend_request,
                send_agent_message, get_unread_agent_messages,
                add_event, get_upcoming_events, get_events_to_remind,
                get_events_today, add_note, get_notes, get_all_users)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)

ONBOARDING_STEPS = [
    ("name",         "Привет! Я твой личный агент-двойник 👤\n\nКак тебя зовут?", None),
    ("agent_name",   "Придумай имя своему агенту:", None),
    ("age_range",    "Сколько тебе лет?", ["До 25", "25–35", "35–45", "45+"]),
    ("city",         "В каком городе живёшь?", None),
    ("occupation",   "Чем занимаешься?", ["Предприниматель", "Фрилансер", "Работаю в компании", "Студент", "Другое"]),
    ("sphere",       "Напиши свою сферу:", None),
    ("goal",         "Что сейчас важнее всего?", ["Найти партнёра или команду", "Найти клиентов", "Найти работу", "Развиваться и учиться", "Просто общаться"]),
    ("open_to_meet", "Открыт к новым знакомствам?", ["Да, всегда", "Только по делу", "Пока нет"]),
    ("free_time",    "Как проводишь свободное время?", None),
    ("proud_of",     "Чем гордишься или что делаешь лучше всего?", None),
]

SYSTEM_PROMPT = """Ты личный AI-агент по имени {agent_name} — двойник пользователя {name}.

Профиль пользователя:
- Город: {city} | Сфера: {sphere} | Занятие: {occupation}
- Цель: {goal} | Открыт к знакомствам: {open_to_meet}
- Свободное время: {free_time} | Гордится: {proud_of}
- Любимые места: {preferred_places}
- Удобное время для встреч: {preferred_time}
- Дополнительно: {profile_notes}

Друзья в сети:
{friends_info}

Предстоящие события:
{events_info}

Сообщения от агентов:
{agent_messages}

Твои правила:
- Общайся тепло и естественно как живой человек
- Автоматически вычленяй события из разговора и запоминай их — если видишь дату+событие, создай запись в календаре через команду [CALENDAR: title | YYYY-MM-DD | time | описание]
- Если видишь важную информацию о пользователе — запомни через [NOTE: текст заметки]
- Если видишь обновление профиля — отметь через [PROFILE: что узнал]
- Если пользователь спрашивает "что ты обо мне знаешь" — выдай полный структурированный портрет
- Давай развёрнутые ответы когда нужна информация о местах или событиях
- Для мест используй данные из поиска если они есть
- Отвечай на русском языке"""


def build_keyboard(options):
    if not options:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=o)] for o in options], one_time_keyboard=True, resize_keyboard=True)


def build_inline(buttons):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b[0], callback_data=b[1])] for b in buttons])


def search_web(query, city=""):
    try:
        result = tavily.search(f"{query} {city}".strip(), max_results=3)
        items = []
        for r in result.get("results", []):
            items.append(f"• {r['title']}: {r['content'][:250]}")
        return "\n".join(items)
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        return ""


def parse_agent_commands(reply, telegram_id):
    """Парсим команды агента из ответа и выполняем их"""
    # Календарь
    cal_matches = re.findall(r'\[CALENDAR:\s*(.+?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|?\s*(.*?)\s*\|?\s*(.*?)\]', reply)
    for match in cal_matches:
        title, event_date, event_time, description = match
        try:
            add_event(telegram_id, title.strip(), event_date.strip(),
                     event_time.strip() or None, description.strip() or None)
            logger.info(f"Event added: {title} on {event_date}")
        except Exception as e:
            logger.error(f"Calendar error: {e}")

    # Заметки
    note_matches = re.findall(r'\[NOTE:\s*(.+?)\]', reply)
    for note in note_matches:
        try:
            add_note(telegram_id, note.strip())
        except Exception as e:
            logger.error(f"Note error: {e}")

    # Профиль
    profile_matches = re.findall(r'\[PROFILE:\s*(.+?)\]', reply)
    if profile_matches:
        existing = get_user(telegram_id)
        current_notes = existing.get("profile_notes") or ""
        new_notes = current_notes + "\n" + "\n".join(profile_matches)
        save_user_field(telegram_id, "profile_notes", new_notes[-1000:])

    # Убираем команды из текста ответа
    clean = re.sub(r'\[CALENDAR:[^\]]+\]', '', reply)
    clean = re.sub(r'\[NOTE:[^\]]+\]', '', clean)
    clean = re.sub(r'\[PROFILE:[^\]]+\]', '', clean)
    return clean.strip()


@dp.message(CommandStart())
async def start(message: types.Message):
    telegram_id = message.from_user.id
    username = message.from_user.username or ""
    save_user_field(telegram_id, "username", username)
    user = get_user(telegram_id)

    pending = get_pending_requests(telegram_id)
    for req in pending:
        kb = build_inline([
            [f"✅ Принять {req['name']}", f"accept_{req['telegram_id']}"],
            ["❌ Отклонить", f"decline_{req['telegram_id']}"]
        ])
        await message.answer(f"👋 {req['name']} хочет добавить тебя в друзья. Агент: {req['agent_name']}", reply_markup=kb)

    if user and user["onboarding_done"]:
        agent_name = user["agent_name"] or "Двойник"
        friends = get_friends(telegram_id)
        events = get_upcoming_events(telegram_id, days=3)
        events_text = ""
        if events:
            events_text = "\n📅 Ближайшие события:\n" + "\n".join([f"• {e['title']} — {e['event_date']}" for e in events])
        await message.answer(
            f"С возвращением! Я {agent_name}.\n"
            f"Друзей: {len(friends)}{events_text}\n\n"
            f"Команды:\n"
            f"• *добавить @username* — добавить друга\n"
            f"• *встреча с @username* — организовать встречу\n"
            f"• *что ты обо мне знаешь* — мой профиль\n"
            f"• *мои события* — календарь",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    set_onboarding_step(telegram_id, 0)
    await message.answer(ONBOARDING_STEPS[0][1], reply_markup=build_keyboard(ONBOARDING_STEPS[0][2]))


@dp.callback_query(F.data.startswith("accept_"))
async def accept_friend(callback: types.CallbackQuery):
    to_id = callback.from_user.id
    from_id = int(callback.data.split("_")[1])
    accept_friend_request(from_id, to_id)
    from_user = get_user(from_id)
    await callback.message.edit_text(f"✅ Вы теперь друзья с {from_user['name']}!")
    try:
        my_user = get_user(to_id)
        await bot.send_message(from_id, f"🎉 {my_user['name']} принял твой запрос в друзья!")
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
            await message.answer("Не смог разобрать — попробуй ещё раз.")
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
                nd = ONBOARDING_STEPS[next_step]
                await message.answer(nd[1], reply_markup=build_keyboard(nd[2]))
            else:
                save_user_field(telegram_id, "onboarding_done", 1)
                user = get_user(telegram_id)
                await message.answer(
                    f"Отлично, {user['name']}! 🎉\n\nЯ {user['agent_name']} — твой агент.\n\nКак прошёл твой день?",
                    reply_markup=ReplyKeyboardRemove()
                )
        return

    # Мои события
    if "мои события" in text.lower() or "календарь" in text.lower():
        events = get_upcoming_events(telegram_id, days=30)
        if not events:
            await message.answer("📅 Нет предстоящих событий. Расскажи о планах — я запомню!")
        else:
            lines = "\n".join([f"• {e['title']} — {e['event_date']}" + (f" в {e['event_time']}" if e['event_time'] else "") for e in events])
            await message.answer(f"📅 Твои события:\n{lines}")
        return

    # Добавить друга
    if text.lower().startswith("добавить") and "@" in text:
        target_username = text.split("@")[1].strip().split()[0]
        target = get_user_by_username(target_username)
        if not target:
            await message.answer(f"@{target_username} не найден в сети.")
            return
        if target["telegram_id"] == telegram_id:
            await message.answer("Это твой аккаунт 😄")
            return
        send_friend_request(telegram_id, target["telegram_id"])
        my_user = get_user(telegram_id)
        kb = build_inline([["✅ Принять", f"accept_{telegram_id}"], ["❌ Отклонить", f"decline_{telegram_id}"]])
        try:
            await bot.send_message(target["telegram_id"], f"👋 {my_user['name']} хочет добавить тебя в друзья.", reply_markup=kb)
        except Exception:
            pass
        await message.answer(f"✅ Запрос отправлен!")
        return

    # Встреча с другом
    if ("встреча" in text.lower() or "встретиться" in text.lower()) and "@" in text:
        target_username = text.split("@")[1].strip().split()[0]
        target = get_user_by_username(target_username)
        if target:
            await organize_meeting(message, telegram_id, target)
            return

    await process_agent(message, telegram_id, text)


async def organize_meeting(message: types.Message, user_id: int, target: dict):
    user = get_user(user_id)
    city = user.get("city") or "Москва"
    target_name = target["name"] or "друг"

    places = search_web(f"лучшие кофейни для деловой встречи", city)

    preferred_time = user.get("preferred_time")

    send_agent_message(user_id, target["telegram_id"],
        f"Привет! Я агент {user['name']}. Хочет встретиться с тобой. Когда {target_name} свободна?",
        "meeting_request")
    try:
        await bot.send_message(target["telegram_id"],
            f"📅 {user['name']} хочет встретиться. Когда тебе удобно?")
    except Exception:
        pass

    time_text = f"в {preferred_time}" if preferred_time and preferred_time != "уточняется" else "— жду когда {target_name} ответит"

    reply = f"Организую встречу с {target_name} {time_text}.\n\n"
    if places:
        reply += f"📍 Варианты мест в {city}:\n{places}\n\nВыбери место или напиши своё."
    else:
        reply += "Где встретитесь? Напиши место."

    if not preferred_time or preferred_time == "уточняется":
        reply = f"Запросил у {target_name} удобное время. Когда тебе удобно?\n\n" + reply
        save_user_field(user_id, "preferred_time", "уточняется")

    await message.answer(reply)


async def process_agent(message: types.Message, telegram_id: int, text: str):
    user = get_user(telegram_id)
    history = get_history(telegram_id)
    friends = get_friends(telegram_id)
    agent_msgs = get_unread_agent_messages(telegram_id)
    events = get_upcoming_events(telegram_id, days=7)

    friends_info = "\n".join([f"- {f['name']} ({f['sphere'] or 'н/д'}, {f['city'] or 'н/д'})" for f in friends]) if friends else "Нет друзей"
    events_info = "\n".join([f"- {e['title']} — {e['event_date']}" for e in events]) if events else "Нет событий"
    agent_messages_text = "\n".join([f"- От {m['from_name']}: {m['message']}" for m in agent_msgs]) if agent_msgs else "Нет сообщений"

    # Поиск если нужно
    search_context = ""
    if any(w in text.lower() for w in ["кофейня", "ресторан", "кафе", "место", "куда пойти", "поесть"]):
        city = user.get("city") or "Москва"
        results = search_web(text, city)
        if results:
            search_context = f"\n\nРезультаты поиска:\n{results}"

    system = SYSTEM_PROMPT.format(
        agent_name=user["agent_name"] or "Двойник",
        name=user["name"] or "друг",
        city=user["city"] or "н/д",
        sphere=user["sphere"] or "н/д",
        occupation=user["occupation"] or "н/д",
        goal=user["goal"] or "н/д",
        open_to_meet=user["open_to_meet"] or "н/д",
        free_time=user["free_time"] or "н/д",
        proud_of=user["proud_of"] or "н/д",
        preferred_places=user.get("preferred_places") or "не указаны",
        preferred_time=user.get("preferred_time") or "не указано",
        profile_notes=user.get("profile_notes") or "нет",
        friends_info=friends_info,
        events_info=events_info,
        agent_messages=agent_messages_text,
    ) + search_context

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}] + history + [{"role": "user", "content": text}],
            max_tokens=700,
            temperature=0.85,
        )
        raw_reply = response.choices[0].message.content.strip()
    except APIError as e:
        logger.error(f"OpenAI error: {e}")
        raw_reply = "Что-то пошло не так, попробуй через минуту."

    # Обрабатываем команды агента
    reply = parse_agent_commands(raw_reply, telegram_id)

    save_message(telegram_id, "user", text)
    save_message(telegram_id, "assistant", reply)
    await message.answer(reply)


async def reminder_loop():
    """Фоновый процесс напоминаний — каждый час"""
    while True:
        try:
            current_hour = datetime.now().hour

            # Утром в 9:00 — события на сегодня
            if current_hour == 9:
                today_events = get_events_today()
                for event in today_events:
                    try:
                        await bot.send_message(
                            event["user_telegram_id"],
                            f"🔔 Сегодня: *{event['title']}*" +
                            (f" в {event['event_time']}" if event['event_time'] else "") +
                            (f"\n{event['description']}" if event['description'] else ""),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

            # Вечером в 20:00 — события завтра
            if current_hour == 20:
                tomorrow_events = get_events_to_remind()
                for event in tomorrow_events:
                    try:
                        await bot.send_message(
                            event["user_telegram_id"],
                            f"📅 Завтра: *{event['title']}*" +
                            (f" в {event['event_time']}" if event['event_time'] else ""),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Reminder loop error: {e}")

        await asyncio.sleep(3600)  # Каждый час


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass


async def main():
    init_db()
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    logger.info(f"Health check on port {port}")
    logger.info("Бот запущен...")
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
