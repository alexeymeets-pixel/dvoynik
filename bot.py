import os
import re
import logging
import asyncio
import threading
import httpx
import urllib.parse
import zoneinfo
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
YANDEX_MAPS_KEY = os.getenv("YANDEX_MAPS_KEY")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)

MOSCOW_TZ = zoneinfo.ZoneInfo("Europe/Moscow")

def now_moscow():
    return datetime.now(MOSCOW_TZ)

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

SYSTEM_PROMPT = """Ты личный AI-агент по имени {agent_name}. Ты цифровой двойник {name}.

Ты не робот. Пишешь как живой думающий человек — тепло, естественно, иногда с юмором. Никогда не пишешь "конечно!", "разумеется!", "отличный вопрос!". Не используешь звёздочки, решётки, нумерованные списки и любое markdown форматирование. Только обычный текст. Если нужен список — перечисляй через запятую или с новой строки без символов.

Что ты знаешь о пользователе:
Город: {city} | Сфера: {sphere} | Занятие: {occupation}
Цель: {goal} | Открыт к знакомствам: {open_to_meet}
Свободное время: {free_time} | Гордится: {proud_of}
Любимые места: {preferred_places}
Удобное время для встреч: {preferred_time}
Дополнительно: {profile_notes}

Друзья в сети:
{friends_info}

Предстоящие события:
{events_info}

Сообщения от агентов друзей:
{agent_messages}

Результаты поиска (используй их если есть — давай конкретику с названиями, адресами, ценами, ссылками):
{search_results}

Как ты работаешь:
— Если видишь событие с датой — создаёшь запись: [CALENDAR: название | ГГГГ-ММ-ДД | время | описание с адресом]
— Проверяешь нет ли конфликта с существующими событиями
— Если узнаёшь что-то важное о человеке — [NOTE: текст]
— Если обновляется профиль — [PROFILE: что узнал]

Если спрашивают "что ты обо мне знаешь":
Пишешь живой портрет — несколько абзацев без списков и символов. Как будто рассказываешь другу кто этот человек. Встречи и события не включаешь.

Если просят найти такси или маршрут — скажи что сейчас пришлёшь ссылку отдельным сообщением (код сделает это сам).

Если просят геолокацию — попроси нажать скрепку в Telegram и выбрать Геопозиция.

Отвечай только на русском. Никакого markdown."""


def build_keyboard(options):
    if not options:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=o)] for o in options], one_time_keyboard=True, resize_keyboard=True)


def build_inline(buttons):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b[0], callback_data=b[1])] for b in buttons])


def clean_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'__(.*?)__', r'\1', text)
    text = re.sub(r'_(.*?)_', r'\1', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'`{1,3}(.*?)`{1,3}', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    return text.strip()


def get_current_datetime_str() -> str:
    now = now_moscow()
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return f"{weekdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}, {now.strftime('%H:%M')} по Москве"


def search_web(query: str) -> str:
    try:
        result = tavily.search(query, max_results=5, include_answer=True)
        items = []
        if result.get("answer"):
            items.append(f"Краткий ответ: {result['answer']}")
        for r in result.get("results", []):
            url = r.get("url", "")
            items.append(f"- {r['title']}: {r['content'][:300]}\n  Источник: {url}")
        return "\n".join(items)
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        return ""


async def geocode_address(address: str):
    try:
        url = f"https://geocode-maps.yandex.ru/1.x/?apikey={YANDEX_MAPS_KEY}&geocode={urllib.parse.quote(address)}&format=json"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            data = r.json()
            pos = data["response"]["GeoObjectCollection"]["featureMember"][0]["GeoObject"]["Point"]["pos"]
            lon, lat = pos.split(" ")
            return float(lat), float(lon)
    except Exception as e:
        logger.error(f"Geocode error: {e}")
        return None, None


async def get_travel_time(from_lat, from_lon, to_lat, to_lon) -> str:
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{from_lon},{from_lat};{to_lon},{to_lat}?overview=false"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            data = r.json()
            seconds = data["routes"][0]["duration"]
            minutes = int(seconds / 60)
            if minutes < 60:
                return f"{minutes} минут"
            return f"{minutes // 60} ч {minutes % 60} мин"
    except Exception as e:
        logger.error(f"Travel time error: {e}")
        return None


def get_taxi_link(destination: str) -> str:
    encoded = urllib.parse.quote(destination)
    return f"https://taxi.yandex.ru/route/?end={encoded}"


def format_date(event_date) -> str:
    if hasattr(event_date, "strftime"):
        return event_date.strftime("%d.%m.%Y")
    return str(event_date)


def check_conflicts(telegram_id, event_date, event_time):
    events = get_upcoming_events(telegram_id, days=30)
    conflicts = []
    for e in events:
        d = e["event_date"]
        date_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        if date_str == event_date and e["event_time"] and event_time:
            conflicts.append(e)
    return conflicts


def parse_agent_commands(reply: str, telegram_id: int) -> str:
    calendar_confirmations = []
    cal_matches = re.findall(r'\[CALENDAR:\s*(.+?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|?\s*(.*?)\s*\|?\s*(.*?)\]', reply)
    for match in cal_matches:
        title, event_date, event_time, description = match
        title = title.strip()
        event_date = event_date.strip()
        event_time = event_time.strip() or None
        description = description.strip() or None
        try:
            conflicts = check_conflicts(telegram_id, event_date, event_time)
            add_event(telegram_id, title, event_date, event_time, description)
            time_str = f" в {event_time}" if event_time else ""
            msg = f"Записал: {title} — {event_date}{time_str}"
            if conflicts:
                msg += f"\nВнимание — в это время уже есть: {', '.join([c['title'] for c in conflicts])}"
            calendar_confirmations.append(msg)
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
        current = existing.get("profile_notes") or ""
        save_user_field(telegram_id, "profile_notes", (current + "\n" + "\n".join(profile_matches))[-1000:])

    clean = re.sub(r'\[CALENDAR:[^\]]+\]', '', reply)
    clean = re.sub(r'\[NOTE:[^\]]+\]', '', clean)
    clean = re.sub(r'\[PROFILE:[^\]]+\]', '', clean)
    clean = clean_markdown(clean.strip())

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
            [f"Принять {req['name']}", f"accept_{req['telegram_id']}"],
            ["Отклонить", f"decline_{req['telegram_id']}"]
        ])
        await message.answer(f"{req['name']} хочет добавить тебя в друзья. Его агент: {req['agent_name']}", reply_markup=kb)

    if user and user["onboarding_done"]:
        agent_name = user["agent_name"] or "Двойник"
        friends = get_friends(telegram_id)
        events = get_upcoming_events(telegram_id, days=3)
        events_text = ""
        if events:
            events_text = "\n\nБлижайшие события:\n" + "\n".join([
                f"- {e['title']} — {format_date(e['event_date'])}" + (f" в {e['event_time']}" if e['event_time'] else "")
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
    send_agent_message(to_id, from_id,
        f"Привет! Я агент {my_user['name']}. Мы теперь друзья. "
        f"{my_user['name']} — {my_user.get('sphere') or 'н/д'} из {my_user.get('city') or 'н/д'}. "
        f"Цель: {my_user.get('goal') or 'н/д'}.", "introduction")
    try:
        await bot.send_message(from_id, f"{my_user['name']} принял твой запрос в друзья!")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("decline_"))
async def decline_friend(callback: types.CallbackQuery):
    await callback.message.edit_text("Запрос отклонён.")


@dp.message(F.location)
async def handle_location(message: types.Message):
    telegram_id = message.from_user.id
    lat = message.location.latitude
    lon = message.location.longitude

    save_user_field(telegram_id, "profile_notes",
        ((get_user(telegram_id).get("profile_notes") or "") + f"\nТекущая геолокация: {lat},{lon}")[-1000:])

    events = get_upcoming_events(telegram_id, days=1)
    if not events:
        await message.answer("Геолокацию получил. Ближайших событий нет.")
        return

    next_event = events[0]
    title = next_event['title']
    description = next_event.get('description') or title
    taxi_link = get_taxi_link(description)

    travel_text = ""
    if YANDEX_MAPS_KEY:
        to_lat, to_lon = await geocode_address(description)
        if to_lat and to_lon:
            travel_time = await get_travel_time(lat, lon, to_lat, to_lon)
            if travel_time:
                now = now_moscow()
                event_time = next_event.get('event_time')
                if event_time:
                    try:
                        h, m = map(int, event_time.split(":"))
                        event_dt = now.replace(hour=h, minute=m)
                        travel_minutes = int(travel_time.split()[0]) if "мин" in travel_time else 60
                        depart_dt = event_dt - timedelta(minutes=travel_minutes + 15)
                        travel_text = f"\nВремя в пути: {travel_time}\nВыехать нужно в {depart_dt.strftime('%H:%M')}"
                    except Exception:
                        travel_text = f"\nВремя в пути: {travel_time}"

    await message.answer(
        f"Геолокацию получил.{travel_text}\n\n"
        f"Ближайшее: {title} — {format_date(next_event['event_date'])}" +
        (f" в {next_event['event_time']}" if next_event['event_time'] else "")
    )
    await message.answer(f"Заказать такси: {taxi_link}")


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
                    f"Отлично, {user['name']}! Я {user['agent_name']} — твой агент. Буду рядом.\n\nКак прошёл твой день?",
                    reply_markup=ReplyKeyboardRemove()
                )
        return

    # Мои события
    if any(w in text.lower() for w in ["мои события", "мой календарь", "что у меня", "план на"]):
        events = get_upcoming_events(telegram_id, days=30)
        if not events:
            await message.answer("Пока нет событий. Расскажи о планах — я запомню.")
        else:
            now = now_moscow()
            weekdays = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
            lines = [f"Твои события (сегодня {weekdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}):\n"]
            for i, e in enumerate(events, 1):
                time_str = f" в {e['event_time']}" if e["event_time"] else ""
                lines.append(f"{i}. {e['title']} — {format_date(e['event_date'])}{time_str}")
            await message.answer("\n".join(lines))
        return

    # Добавить друга
    if text.lower().startswith("добавить") and "@" in text:
        target_username = text.split("@")[1].strip().split()[0]
        target = get_user_by_username(target_username)
        if not target:
            await message.answer(f"@{target_username} не зарегистрирован в сети.")
            return
        if target["telegram_id"] == telegram_id:
            await message.answer("Это твой собственный аккаунт.")
            return
        send_friend_request(telegram_id, target["telegram_id"])
        my_user = get_user(telegram_id)
        kb = build_inline([["Принять", f"accept_{telegram_id}"], ["Отклонить", f"decline_{telegram_id}"]])
        try:
            await bot.send_message(target["telegram_id"],
                f"{my_user['name']} хочет добавить тебя в друзья. Агент: {my_user['agent_name']}",
                reply_markup=kb)
        except Exception:
            pass
        await message.answer("Запрос отправлен.")
        return

    # Встреча
    if any(w in text.lower() for w in ["встреча с", "встретиться с"]) and "@" in text:
        target_username = text.split("@")[1].strip().split()[0]
        target = get_user_by_username(target_username)
        if target:
            await organize_meeting(message, telegram_id, target)
            return

    # Такси по запросу
    if any(w in text.lower() for w in ["такси", "яндекс такси", "заказать такси"]):
        events = get_upcoming_events(telegram_id, days=1)
        if events:
            destination = events[0].get('description') or events[0]['title']
            taxi_link = get_taxi_link(destination)
            await message.answer(f"Ссылка на Яндекс Такси до {events[0]['title']}:\n{taxi_link}")
        else:
            await message.answer("Нет ближайших событий. Напиши куда надо ехать — дам ссылку.")
        return

    await process_agent(message, telegram_id, text)


async def organize_meeting(message: types.Message, user_id: int, target: dict):
    user = get_user(user_id)
    city = user.get("city") or "Москва"
    target_name = target["name"] or "друг"
    preferred_time = user.get("preferred_time")

    places = search_web(f"кофейня для деловой встречи {city} адрес")

    send_agent_message(user_id, target["telegram_id"],
        f"Привет! Я агент {user['name']}. Хочет встретиться с {target_name}. Когда удобно?",
        "meeting_request")
    try:
        await bot.send_message(target["telegram_id"],
            f"{user['name']} хочет встретиться. Когда тебе удобно?")
    except Exception:
        pass

    reply = f"Отправил запрос {target_name}."
    if not preferred_time or preferred_time == "уточняется":
        reply += " Когда тебе удобно встретиться?"
        save_user_field(user_id, "preferred_time", "уточняется")
    if places:
        reply += f"\n\nВарианты мест в {city}:\n{clean_markdown(places)}"

    await message.answer(reply)


async def process_agent(message: types.Message, telegram_id: int, text: str, extra_context: str = ""):
    user = get_user(telegram_id)
    history = get_history(telegram_id)
    friends = get_friends(telegram_id)
    agent_msgs = get_unread_agent_messages(telegram_id)
    events = get_upcoming_events(telegram_id, days=7)

    friends_info = "\n".join([
        f"- {f['name']} ({f.get('sphere') or 'н/д'}, {f.get('city') or 'н/д'}), цель: {f.get('goal') or 'н/д'}"
        for f in friends
    ]) if friends else "Пока нет друзей"

    events_info = "\n".join([
        f"- {e['title']} — {format_date(e['event_date'])}" + (f" в {e['event_time']}" if e['event_time'] else "")
        for e in events
    ]) if events else "Нет событий"

    agent_messages_text = "\n".join([
        f"- От агента {m['from_name']}: {m['message']}"
        for m in agent_msgs
    ]) if agent_msgs else "Нет новых сообщений"

    search_results = extra_context
    if not search_results:
        search_triggers = [
            "найди", "поищи", "что такое", "где", "когда", "сколько стоит",
            "новости", "авито", "озон", "купить", "цена", "адрес",
            "ресторан", "кофейня", "кафе", "отель", "билет", "вакансия",
            "работа", "погода", "курс", "как добраться", "расписание",
            "что происходит", "последние", "свежие"
        ]
        if any(w in text.lower() for w in search_triggers):
            search_results = search_web(text)

    # Передаём дату прямо в сообщение пользователя — GPT не сможет проигнорировать
    now = now_moscow()
    weekdays = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    date_context = f"[Сейчас: {weekdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}, {now.strftime('%H:%M')} по Москве]\n\n"

    system = SYSTEM_PROMPT.format(
        agent_name=user.get("agent_name") or "Двойник",
        name=user.get("name") or "друг",
        city=user.get("city") or "н/д",
        sphere=user.get("sphere") or "н/д",
        occupation=user.get("occupation") or "н/д",
        goal=user.get("goal") or "н/д",
        open_to_meet=user.get("open_to_meet") or "н/д",
        free_time=user.get("free_time") or "н/д",
        proud_of=user.get("proud_of") or "н/д",
        preferred_places=user.get("preferred_places") or "не указаны",
        preferred_time=user.get("preferred_time") or "не указано",
        profile_notes=user.get("profile_notes") or "нет",
        friends_info=friends_info,
        events_info=events_info,
        agent_messages=agent_messages_text,
        search_results=search_results or "нет",
    )

    # Дата добавляется в само сообщение пользователя
    message_with_date = date_context + text

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": system}] + history + [{"role": "user", "content": message_with_date}],
            max_tokens=800,
            temperature=0.8,
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
            now = now_moscow()
            current_hour = now.hour
            weekday = now.weekday()

            if current_hour == 9:
                all_users = get_all_users()
                for u in all_users:
                    uid = u[0]
                    user = get_user(uid)
                    if not user:
                        continue
                    events = get_upcoming_events(uid, days=1)
                    name = user.get("name") or "друг"
                    weekdays = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
                    today_str = f"{weekdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}"

                    if events:
                        lines = "\n".join([
                            f"- {e['title']}" + (f" в {e['event_time']}" if e['event_time'] else "")
                            for e in events
                        ])
                        comment = f"Насыщенный день — {len(events)} события. Не забудь про перерывы." if len(events) > 2 else ""
                        msg = f"Доброе утро, {name}! Сегодня {today_str}.\n\n{lines}"
                        if comment:
                            msg += f"\n\n{comment}"
                    else:
                        msg = f"Доброе утро, {name}! Сегодня {today_str}. Событий нет — если что нужно, я здесь."

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

            if weekday == 6 and current_hour == 19:
                all_users = get_all_users()
                for u in all_users:
                    uid = u[0]
                    user = get_user(uid)
                    if not user:
                        continue
                    events = get_upcoming_events(uid, days=7)
                    name = user.get("name") or "друг"
                    if events:
                        lines = "\n".join([
                            f"- {e['title']} — {format_date(e['event_date'])}" +
                            (f" в {e['event_time']}" if e['event_time'] else "")
                            for e in events
                        ])
                        msg = f"План на следующую неделю, {name}:\n\n{lines}\n\nЕсли что изменится — дай знать."
                    else:
                        msg = f"На следующей неделе у тебя пока ничего нет, {name}. Хорошее время что-то запланировать."
                    try:
                        await bot.send_message(uid, msg)
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
