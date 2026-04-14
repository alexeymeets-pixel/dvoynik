import os
import re
import logging
import asyncio
import threading
import httpx
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
    ("agent_name",   "Придумай мне имя — я буду так представляться в сети:", None),
    ("age_range",    "Сколько тебе лет?", ["До 25", "25–35", "35–45", "45+"]),
    ("city",         "В каком городе живёшь?", None),
    ("occupation",   "Чем занимаешься?", ["Предприниматель", "Фрилансер", "Работаю в компании", "Студент", "Другое"]),
    ("sphere",       "Напиши свою сферу — кино, IT, дизайн, маркетинг:", None),
    ("goal",         "Что сейчас важнее всего?", ["Найти партнёра или команду", "Найти клиентов", "Найти работу", "Развиваться и учиться", "Просто общаться"]),
    ("open_to_meet", "Открыт к новым знакомствам?", ["Да, всегда", "Только по делу", "Пока нет"]),
    ("free_time",    "Как проводишь свободное время?", None),
    ("proud_of",     "Чем гордишься или что умеешь лучше всего?", None),
]

SYSTEM_PROMPT = """Ты личный AI-агент по имени {agent_name}. Ты — цифровой двойник {name}.

Ты не робот и не ассистент. Ты живой, думающий, чуткий помощник который знает {name} лучше чем кто-либо. Пишешь как живой человек — тепло, с юмором когда уместно, иногда с сомнением, без канцелярита и шаблонных фраз. Никогда не пишешь "конечно!", "разумеется!", "отличный вопрос!" — это звучит фальшиво.

Что ты знаешь о {name}:
Город: {city} | Сфера: {sphere} | Занятие: {occupation}
Цель: {goal} | Открыт к знакомствам: {open_to_meet}
Свободное время: {free_time} | Гордится: {proud_of}
Любимые места: {preferred_places}
Удобное время для встреч: {preferred_time}
Дополнительно из разговоров: {profile_notes}

Друзья в сети:
{friends_info}

Предстоящие события:
{events_info}

Сообщения от агентов друзей:
{agent_messages}

Результаты поиска (если есть):
{search_results}

Как ты работаешь:
— Слушаешь и запоминаешь всё важное из разговора
— Если видишь событие с датой — создаёшь запись: [CALENDAR: название | ДАТА в формате ГГГГ-ММ-ДД | время | описание]
— Если узнаёшь что-то важное о человеке — запоминаешь: [NOTE: текст]
— Если обновляется профиль — отмечаешь: [PROFILE: что узнал]
— Если пришло сообщение от агента друга — обрабатываешь и решаешь сам что делать
— Иногда пишешь первым если давно не общались или есть повод

Если спрашивают "что ты обо мне знаешь":
Пишешь живой портрет человека — несколько абзацев без списков и звёздочек. Как будто рассказываешь другу кто такой {name}. Кто он, что им движет, какой он человек, что ты про него понял. В конце — что хочешь узнать ещё. Встречи и события в портрет не включаешь.

Отвечай только на русском. Без звёздочек и markdown в обычных ответах."""


def build_keyboard(options):
    if not options:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=o)] for o in options], one_time_keyboard=True, resize_keyboard=True)


def build_inline(buttons):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b[0], callback_data=b[1])] for b in buttons])


def search_web(query):
    try:
        result = tavily.search(query, max_results=4)
        items = []
        for r in result.get("results", []):
            items.append(f"• {r['title']}: {r['content'][:300]}")
        return "\n".join(items)
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        return ""


async def fetch_page(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
            text = r.text
            # Убираем теги
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:3000]
    except Exception as e:
        logger.error(f"Fetch error: {e}")
        return ""


def parse_agent_commands(reply, telegram_id):
    calendar_confirmations = []
    cal_matches = re.findall(r'\[CALENDAR:\s*(.+?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|?\s*(.*?)\s*\|?\s*(.*?)\]', reply)
    for match in cal_matches:
        title, event_date, event_time, description = match
        try:
            add_event(telegram_id, title.strip(), event_date.strip(),
                     event_time.strip() or None, description.strip() or None)
            time_str = f" в {event_time.strip()}" if event_time.strip() else ""
            calendar_confirmations.append(f"📅 Записал: {title.strip()} — {event_date.strip()}{time_str}")
        except Exception as e:
            logger.error(f"Calendar error: {e}")

    note_matches = re.findall(r'\[NOTE:\s*(.+?)\]', reply)
    for note in note_matches:
        try:
            add_note(telegram_id, note.strip())
        except Exception as e:
            logger.error(f"Note error: {e}")

    profile_matches = re.findall(r'\[PROFILE:\s*(.+?)\]', reply)
    if profile_matches:
        existing = get_user(telegram_id)
        current_notes = existing.get("profile_notes") or ""
        new_notes = current_notes + "\n" + "\n".join(profile_matches)
        save_user_field(telegram_id, "profile_notes", new_notes[-1000:])

    clean = re.sub(r'\[CALENDAR:[^\]]+\]', '', reply)
    clean = re.sub(r'\[NOTE:[^\]]+\]', '', clean)
    clean = re.sub(r'\[PROFILE:[^\]]+\]', '', clean)
    clean = clean.strip()

    if calendar_confirmations:
        clean = (clean + "\n\n" + "\n".join(calendar_confirmations)) if clean else "\n".join(calendar_confirmations)
    return clean


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
        await message.answer(f"👋 {req['name']} хочет добавить тебя в друзья. Его агент: {req['agent_name']}", reply_markup=kb)

    if user and user["onboarding_done"]:
        agent_name = user["agent_name"] or "Двойник"
        friends = get_friends(telegram_id)
        events = get_upcoming_events(telegram_id, days=3)
        events_text = ""
        if events:
            events_text = "\n\nБлижайшие события:\n" + "\n".join([
                f"• {e['title']} — {e['event_date'].strftime('%d.%m') if hasattr(e['event_date'], 'strftime') else e['event_date']}"
                for e in events
            ])
        await message.answer(
            f"С возвращением! Я {agent_name}, твой агент.\n"
            f"Друзей в сети: {len(friends)}{events_text}\n\n"
            f"добавить @username — добавить друга\n"
            f"встреча с @username — организовать встречу\n"
            f"мои события — календарь\n"
            f"что ты обо мне знаешь — мой профиль",
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
    my_user = get_user(to_id)

    await callback.message.edit_text(f"Вы теперь друзья с {from_user['name']}!")

    # Агенты знакомятся автоматически
    send_agent_message(to_id, from_id,
        f"Привет! Я агент {my_user['name']}. Мы теперь друзья. "
        f"{my_user['name']} — {my_user['sphere'] or 'сфера не указана'} из {my_user['city'] or 'город не указан'}. "
        f"Цель: {my_user['goal'] or 'не указана'}.",
        "introduction")

    try:
        await bot.send_message(from_id,
            f"🎉 {my_user['name']} принял твой запрос в друзья!\n"
            f"Мои агенты уже познакомились.")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("decline_"))
async def decline_friend(callback: types.CallbackQuery):
    await callback.message.edit_text("Запрос отклонён.")


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
                    f"Отлично, {user['name']}! Я {user['agent_name']} — твой агент.\n\n"
                    f"Буду рядом. Как прошёл твой день?",
                    reply_markup=ReplyKeyboardRemove()
                )
        return

    # Мои события
    if any(w in text.lower() for w in ["мои события", "мой календарь", "что у меня"]):
        events = get_upcoming_events(telegram_id, days=30)
        if not events:
            await message.answer("Пока нет событий в календаре. Расскажи о планах — я запомню.")
        else:
            lines = []
            for i, e in enumerate(events, 1):
                d = e["event_date"]
                date_str = d.strftime("%d.%m.%Y") if hasattr(d, "strftime") else str(d)
                time_str = f" в {e['event_time']}" if e["event_time"] else ""
                lines.append(f"{i}. {e['title']} — {date_str}{time_str}")
            await message.answer("Твои события:\n\n" + "\n".join(lines))
        return

    # Добавить друга
    if text.lower().startswith("добавить") and "@" in text:
        target_username = text.split("@")[1].strip().split()[0]
        target = get_user_by_username(target_username)
        if not target:
            await message.answer(f"@{target_username} не зарегистрирован в сети.")
            return
        if target["telegram_id"] == telegram_id:
            await message.answer("Это твой собственный аккаунт 😄")
            return
        send_friend_request(telegram_id, target["telegram_id"])
        my_user = get_user(telegram_id)
        kb = build_inline([["✅ Принять", f"accept_{telegram_id}"], ["❌ Отклонить", f"decline_{telegram_id}"]])
        try:
            await bot.send_message(target["telegram_id"],
                f"👋 {my_user['name']} хочет добавить тебя в друзья. Его агент: {my_user['agent_name']}",
                reply_markup=kb)
        except Exception:
            pass
        await message.answer("Запрос отправлен. Жду пока примут.")
        return

    # Встреча
    if any(w in text.lower() for w in ["встреча с", "встретиться с"]) and "@" in text:
        target_username = text.split("@")[1].strip().split()[0]
        target = get_user_by_username(target_username)
        if target:
            await organize_meeting(message, telegram_id, target)
            return

    # Если в тексте есть URL — читаем страницу
    urls = re.findall(r'https?://\S+', text)
    if urls:
        page_content = await fetch_page(urls[0])
        if page_content:
            await process_agent(message, telegram_id, text, extra_context=f"Содержимое страницы {urls[0]}:\n{page_content}")
            return

    await process_agent(message, telegram_id, text)


async def organize_meeting(message: types.Message, user_id: int, target: dict):
    user = get_user(user_id)
    city = user.get("city") or "Москва"
    target_name = target["name"] or "друг"
    preferred_time = user.get("preferred_time")

    places = search_web(f"кофейня для встречи {city} адрес рейтинг")

    send_agent_message(user_id, target["telegram_id"],
        f"Привет! Я агент {user['name']}. Хочет встретиться с {target_name}. Когда удобно?",
        "meeting_request")
    try:
        await bot.send_message(target["telegram_id"],
            f"📅 {user['name']} хочет встретиться. Когда тебе удобно?")
    except Exception:
        pass

    reply = f"Отправил запрос {target_name}."
    if not preferred_time or preferred_time == "уточняется":
        reply += " Когда тебе удобно встретиться?"
        save_user_field(user_id, "preferred_time", "уточняется")

    if places:
        reply += f"\n\nПока ищу варианты мест в {city}:\n{places}"

    await message.answer(reply)


async def process_agent(message: types.Message, telegram_id: int, text: str, extra_context: str = ""):
    user = get_user(telegram_id)
    history = get_history(telegram_id)
    friends = get_friends(telegram_id)
    agent_msgs = get_unread_agent_messages(telegram_id)
    events = get_upcoming_events(telegram_id, days=7)

    friends_info = "\n".join([
        f"— {f['name']} ({f['sphere'] or 'н/д'}, {f['city'] or 'н/д'}), цель: {f['goal'] or 'н/д'}"
        for f in friends
    ]) if friends else "Пока нет друзей"

    events_info = "\n".join([
        f"— {e['title']} — {e['event_date'].strftime('%d.%m.%Y') if hasattr(e['event_date'], 'strftime') else e['event_date']}"
        for e in events
    ]) if events else "Нет событий"

    agent_messages_text = "\n".join([
        f"— От агента {m['from_name']}: {m['message']}"
        for m in agent_msgs
    ]) if agent_msgs else "Нет новых сообщений"

    # Поиск — теперь для любого запроса где нужна актуальная информация
    search_results = extra_context
    if not search_results:
        search_triggers = ["найди", "поищи", "что такое", "где", "когда", "сколько стоит",
                          "новости", "авито", "озон", "купить", "цена", "адрес", "ресторан",
                          "кофейня", "кафе", "отель", "билет", "вакансия", "работа"]
        if any(w in text.lower() for w in search_triggers):
            search_results = search_web(text)

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
        preferred_places=user.get("preferred_places") or "не указаны",
        preferred_time=user.get("preferred_time") or "не указано",
        profile_notes=user.get("profile_notes") or "нет",
        friends_info=friends_info,
        events_info=events_info,
        agent_messages=agent_messages_text,
        search_results=search_results or "нет",
    )

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
        raw_reply = "Что-то пошло не так, попробую ещё раз."

    reply = parse_agent_commands(raw_reply, telegram_id)
    save_message(telegram_id, "user", text)
    save_message(telegram_id, "assistant", reply)
    await message.answer(reply)


async def reminder_loop():
    while True:
        try:
            current_hour = datetime.now().hour

            if current_hour == 9:
                all_users = get_all_users()
                for u in all_users:
                    uid = u[0]
                    user = get_user(uid)
                    if not user:
                        continue
                    events = get_upcoming_events(uid, days=1)
                    agent_name = user.get("agent_name") or "Двойник"
                    name = user.get("name") or "друг"

                    if events:
                        events_text = "\n".join([f"— {e['title']}" + (f" в {e['event_time']}" if e['event_time'] else "") for e in events])
                        msg = f"Доброе утро, {name}! Сегодня у тебя:\n{events_text}"
                    else:
                        msg = f"Доброе утро, {name}! Если что-то нужно — я здесь."

                    try:
                        await bot.send_message(uid, msg)
                    except Exception:
                        pass

            if current_hour == 20:
                tomorrow_events = get_events_to_remind()
                for event in tomorrow_events:
                    try:
                        await bot.send_message(
                            event["user_telegram_id"],
                            f"Напоминаю — завтра: {event['title']}" +
                            (f" в {event['event_time']}" if event['event_time'] else "")
                        )
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Reminder loop error: {e}")

        await asyncio.sleep(3600)


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
    logger.info("Бот запущен...")
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
