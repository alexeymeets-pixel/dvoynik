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
                get_events_today, add_note, get_notes, get_all_users,
                init_projects_and_tasks, add_project, get_projects,
                get_project_by_name, add_task, get_tasks, complete_task,
                get_overdue_tasks)

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

Пиши как живой человек — тепло, по-человечески, иногда с лёгким юмором. Без казённых фраз типа "конечно!", "разумеется!", "дай знать!", "сообщи!". Никогда не заканчивай сообщение дежурной фразой — просто говори что думаешь и замолкай.

Форматирование: используй HTML теги для оформления.
Жирный: <b>текст</b>
Курсив: <i>текст</i>
Разделитель: пустая строка между блоками
Списки: через эмодзи или тире, не через markdown символы.
Никаких звёздочек, решёток, квадратных скобок в тексте ответа.

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

Активные задачи и проекты:
{tasks_info}

Сообщения от агентов друзей:
{agent_messages}

Результаты поиска (используй конкретные данные — названия, адреса, цены, ссылки):
{search_results}

Как ты работаешь:
— Если видишь событие с датой — [CALENDAR: название | ГГГГ-ММ-ДД | время | адрес/описание]
— Если видишь задачу — [TASK: название | проект или пусто | приоритет: high/normal/low | срок ГГГГ-ММ-ДД или пусто]
— Если видишь новый проект — [PROJECT: название | описание]
— Если задача выполнена — [DONE: id задачи]
— Если узнаёшь что-то важное о человеке — [NOTE: текст]
— Если обновляется профиль — [PROFILE: что узнал]

Если спрашивают "что ты обо мне знаешь":
Пишешь живой портрет — несколько абзацев без списков. Как будто рассказываешь другу кто этот человек. Встречи и события не включаешь.

Если просят такси — скажи что сейчас пришлёшь ссылку.
Если просят геолокацию — попроси нажать скрепку и выбрать Геопозиция.

Отвечай только на русском."""


def build_keyboard(options):
    if not options:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=o)] for o in options], one_time_keyboard=True, resize_keyboard=True)


def build_inline(buttons):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b[0], callback_data=b[1])] for b in buttons])


def clean_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'`{1,3}(.*?)`{1,3}', r'\1', text, flags=re.DOTALL)
    return text.strip()


def format_date(event_date) -> str:
    if hasattr(event_date, "strftime"):
        return event_date.strftime("%d.%m.%Y")
    return str(event_date)


def search_web(query: str) -> str:
    try:
        result = tavily.search(query, max_results=5, include_answer=True)
        items = []
        if result.get("answer"):
            items.append(f"Краткий ответ: {result['answer']}")
        for r in result.get("results", []):
            url = r.get("url", "")
            items.append(f"- {r['title']}: {r['content'][:300]}\n  {url}")
        return "\n".join(items)
    except Exception as e:
        logger.error(f"Tavily error: {e}")
        return ""


async def geocode_address(address: str):
    if not YANDEX_MAPS_KEY:
        return None, None
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
                return f"{minutes} мин"
            return f"{minutes // 60} ч {minutes % 60} мин"
    except Exception as e:
        logger.error(f"Travel time error: {e}")
        return None


def get_taxi_link(destination: str) -> str:
    encoded = urllib.parse.quote(destination)
    return f"https://taxi.yandex.ru/route/?end={encoded}"


def check_conflicts(telegram_id, event_date, event_time):
    events = get_upcoming_events(telegram_id, days=30)
    conflicts = []
    for e in events:
        d = e["event_date"]
        date_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        if date_str == event_date and e["event_time"] and event_time:
            if e["event_time"][:5] == event_time[:5]:
                conflicts.append(e)
    return conflicts


def parse_agent_commands(reply: str, telegram_id: int) -> tuple:
    """Возвращает (чистый текст, список подтверждений)"""
    confirmations = []

    # Календарь
    cal_matches = re.findall(r'\[CALENDAR:\s*(.+?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|?\s*(.*?)\s*\|?\s*(.*?)\]', reply)
    for match in cal_matches:
        title, event_date, event_time, description = [x.strip() for x in match]
        event_time = event_time or None
        description = description or None
        try:
            conflicts = check_conflicts(telegram_id, event_date, event_time)
            add_event(telegram_id, title, event_date, event_time, description)
            time_str = f" в {event_time}" if event_time else ""
            msg = f"📅 Записал: <b>{title}</b> — {event_date}{time_str}"
            if conflicts:
                msg += f"\n⚠️ Конфликт с: {', '.join([c['title'] for c in conflicts])}"
            confirmations.append(msg)
        except Exception as e:
            logger.error(f"Calendar error: {e}")

    # Задачи
    task_matches = re.findall(r'\[TASK:\s*(.+?)\s*\|\s*(.*?)\s*\|\s*приоритет:\s*(\w+)\s*\|\s*(.*?)\]', reply)
    for match in task_matches:
        title, project_name, priority, due_date = [x.strip() for x in match]
        try:
            project_id = None
            if project_name:
                project = get_project_by_name(telegram_id, project_name)
                if project:
                    project_id = project['id']
            due = due_date if re.match(r'\d{4}-\d{2}-\d{2}', due_date) else None
            add_task(telegram_id, title, project_id, priority, due)
            proj_str = f" → {project_name}" if project_name else ""
            confirmations.append(f"✅ Задача: <b>{title}</b>{proj_str}")
        except Exception as e:
            logger.error(f"Task error: {e}")

    # Проекты
    proj_matches = re.findall(r'\[PROJECT:\s*(.+?)\s*\|\s*(.*?)\]', reply)
    for match in proj_matches:
        name, description = [x.strip() for x in match]
        try:
            add_project(telegram_id, name, description or None)
            confirmations.append(f"🗂 Проект создан: <b>{name}</b>")
        except Exception as e:
            logger.error(f"Project error: {e}")

    # Выполненные задачи
    done_matches = re.findall(r'\[DONE:\s*(\d+)\]', reply)
    for task_id in done_matches:
        try:
            complete_task(int(task_id))
        except Exception as e:
            logger.error(f"Done task error: {e}")

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
        current = existing.get("profile_notes") or ""
        save_user_field(telegram_id, "profile_notes", (current + "\n" + "\n".join(profile_matches))[-1000:])

    # Чистим текст
    clean = re.sub(r'\[CALENDAR:[^\]]+\]', '', reply)
    clean = re.sub(r'\[TASK:[^\]]+\]', '', clean)
    clean = re.sub(r'\[PROJECT:[^\]]+\]', '', clean)
    clean = re.sub(r'\[DONE:[^\]]+\]', '', clean)
    clean = re.sub(r'\[NOTE:[^\]]+\]', '', clean)
    clean = re.sub(r'\[PROFILE:[^\]]+\]', '', clean)
    clean = clean_markdown(clean.strip())

    return clean, confirmations


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
        await message.answer(
            f"👋 <b>{req['name']}</b> хочет добавить тебя в друзья.\nЕго агент: {req['agent_name']}",
            reply_markup=kb, parse_mode="HTML"
        )

    if user and user["onboarding_done"]:
        agent_name = user["agent_name"] or "Двойник"
        friends = get_friends(telegram_id)
        events = get_upcoming_events(telegram_id, days=3)
        tasks = get_tasks(telegram_id)

        events_text = ""
        if events:
            events_text = "\n\n<b>Ближайшие события:</b>\n" + "\n".join([
                f"📅 {e['title']} — {format_date(e['event_date'])}" + (f" в {e['event_time']}" if e['event_time'] else "")
                for e in events
            ])

        tasks_text = ""
        if tasks:
            tasks_text = f"\n\n<b>Активных задач:</b> {len(tasks)}"

        await message.answer(
            f"С возвращением! Я <b>{agent_name}</b>.\n"
            f"Друзей в сети: {len(friends)}{events_text}{tasks_text}\n\n"
            f"<i>добавить @username — друг\n"
            f"встреча с @username — встреча\n"
            f"мои события — календарь\n"
            f"мои задачи — задачи\n"
            f"что ты обо мне знаешь — профиль</i>",
            reply_markup=ReplyKeyboardRemove(), parse_mode="HTML"
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
    await callback.message.edit_text(f"✅ Вы теперь друзья с <b>{from_user['name']}</b>!", parse_mode="HTML")
    send_agent_message(to_id, from_id,
        f"Привет! Я агент {my_user['name']}. Мы теперь друзья. "
        f"{my_user['name']} — {my_user.get('sphere') or 'н/д'} из {my_user.get('city') or 'н/д'}. "
        f"Цель: {my_user.get('goal') or 'н/д'}.", "introduction")
    try:
        await bot.send_message(from_id, f"🎉 <b>{my_user['name']}</b> принял твой запрос в друзья!", parse_mode="HTML")
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
        ((get_user(telegram_id).get("profile_notes") or "") + f"\nГеолокация: {lat},{lon}")[-1000:])

    events = get_upcoming_events(telegram_id, days=1)
    if not events:
        await message.answer("Геолокацию получил. Ближайших событий нет.")
        return

    next_event = events[0]
    title = next_event['title']
    description = next_event.get('description') or title
    taxi_link = get_taxi_link(description)

    travel_text = ""
    depart_text = ""
    if YANDEX_MAPS_KEY:
        to_lat, to_lon = await geocode_address(description)
        if to_lat and to_lon:
            travel_time = await get_travel_time(lat, lon, to_lat, to_lon)
            if travel_time:
                travel_text = f"\nВремя в пути: <b>{travel_time}</b>"
                event_time = next_event.get('event_time')
                if event_time:
                    try:
                        h, m = map(int, event_time.split(":"))
                        now = now_moscow()
                        event_dt = now.replace(hour=h, minute=m, second=0)
                        mins = int(travel_time.split()[0])
                        depart_dt = event_dt - timedelta(minutes=mins + 15)
                        depart_text = f"\nВыехать нужно в <b>{depart_dt.strftime('%H:%M')}</b>"
                    except Exception:
                        pass

    await message.answer(
        f"📍 Геолокацию получил.{travel_text}{depart_text}\n\n"
        f"Ближайшее: <b>{title}</b> — {format_date(next_event['event_date'])}" +
        (f" в {next_event['event_time']}" if next_event['event_time'] else ""),
        parse_mode="HTML"
    )
    await message.answer(f"🚕 Яндекс Такси: {taxi_link}")


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
                    f"Отлично, <b>{user['name']}</b>! Я <b>{user['agent_name']}</b> — твой агент. Буду рядом.\n\nКак прошёл твой день?",
                    reply_markup=ReplyKeyboardRemove(), parse_mode="HTML"
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
            lines = [f"<b>События (сегодня {weekdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}):</b>\n"]
            for i, e in enumerate(events, 1):
                time_str = f" в {e['event_time']}" if e["event_time"] else ""
                lines.append(f"{i}. {e['title']} — {format_date(e['event_date'])}{time_str}")
            await message.answer("\n".join(lines), parse_mode="HTML")
        return

    # Мои задачи
    if any(w in text.lower() for w in ["мои задачи", "список задач", "что нужно сделать"]):
        tasks = get_tasks(telegram_id)
        projects = get_projects(telegram_id)
        if not tasks and not projects:
            await message.answer("Задач и проектов пока нет. Просто расскажи что нужно сделать — я запишу.")
        else:
            lines = []
            if projects:
                lines.append("<b>Проекты:</b>")
                for p in projects:
                    project_tasks = get_tasks(telegram_id, p['id'])
                    lines.append(f"\n🗂 <b>{p['name']}</b>")
                    for t in project_tasks:
                        lines.append(f"  — {t['title']}" + (f" (до {format_date(t['due_date'])})" if t.get('due_date') else ""))

            standalone = [t for t in tasks if not t.get('project_id')]
            if standalone:
                lines.append("\n<b>Задачи без проекта:</b>")
                for t in standalone:
                    lines.append(f"— {t['title']}" + (f" (до {format_date(t['due_date'])})" if t.get('due_date') else ""))

            await message.answer("\n".join(lines), parse_mode="HTML")
        return

    # Такси
    if any(w in text.lower() for w in ["такси", "яндекс такси", "заказать такси", "вызвать такси"]):
        events = get_upcoming_events(telegram_id, days=1)
        if events:
            destination = events[0].get('description') or events[0]['title']
            taxi_link = get_taxi_link(destination)
            await message.answer(
                f"🚕 Яндекс Такси до <b>{events[0]['title']}</b>:\n{taxi_link}",
                parse_mode="HTML"
            )
        else:
            await message.answer("Нет ближайших событий. Напиши куда ехать — дам ссылку.")
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
                f"👋 <b>{my_user['name']}</b> хочет добавить тебя в друзья. Агент: {my_user['agent_name']}",
                reply_markup=kb, parse_mode="HTML")
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
            f"📅 <b>{user['name']}</b> хочет встретиться. Когда тебе удобно?",
            parse_mode="HTML")
    except Exception:
        pass

    reply = f"Отправил запрос {target_name}."
    if not preferred_time or preferred_time == "уточняется":
        reply += " Когда тебе удобно встретиться?"
        save_user_field(user_id, "preferred_time", "уточняется")
    if places:
        reply += f"\n\n<b>Варианты мест в {city}:</b>\n{clean_markdown(places)}"

    await message.answer(reply, parse_mode="HTML")


async def process_agent(message: types.Message, telegram_id: int, text: str, extra_context: str = ""):
    user = get_user(telegram_id)
    history = get_history(telegram_id)
    friends = get_friends(telegram_id)
    agent_msgs = get_unread_agent_messages(telegram_id)
    events = get_upcoming_events(telegram_id, days=7)
    tasks = get_tasks(telegram_id)
    projects = get_projects(telegram_id)

    friends_info = "\n".join([
        f"- {f['name']} ({f.get('sphere') or 'н/д'}, {f.get('city') or 'н/д'}), цель: {f.get('goal') or 'н/д'}"
        for f in friends
    ]) if friends else "Пока нет друзей"

    events_info = "\n".join([
        f"- {e['title']} — {format_date(e['event_date'])}" + (f" в {e['event_time']}" if e['event_time'] else "")
        for e in events
    ]) if events else "Нет событий"

    tasks_lines = []
    for p in projects:
        ptasks = get_tasks(telegram_id, p['id'])
        if ptasks:
            tasks_lines.append(f"Проект {p['name']}:")
            for t in ptasks:
                tasks_lines.append(f"  - [{t['id']}] {t['title']}" + (f" (до {format_date(t['due_date'])})" if t.get('due_date') else ""))
    standalone = [t for t in tasks if not t.get('project_id')]
    if standalone:
        tasks_lines.append("Без проекта:")
        for t in standalone:
            tasks_lines.append(f"  - [{t['id']}] {t['title']}")
    tasks_info = "\n".join(tasks_lines) if tasks_lines else "Нет активных задач"

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
            "что происходит", "последние", "свежие", "сегодня"
        ]
        if any(w in text.lower() for w in search_triggers):
            search_results = search_web(text)

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
        tasks_info=tasks_info,
        agent_messages=agent_messages_text,
        search_results=search_results or "нет",
    )

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

    reply, confirmations = parse_agent_commands(raw_reply, telegram_id)
    save_message(telegram_id, "user", text)
    save_message(telegram_id, "assistant", reply)

    if reply:
        await message.answer(reply, parse_mode="HTML")
    if confirmations:
        await message.answer("\n".join(confirmations), parse_mode="HTML")


async def reminder_loop():
    while True:
        try:
            now = now_moscow()
            current_hour = now.hour
            weekday = now.weekday()
            weekdays = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]

            if current_hour == 9:
                all_users = get_all_users()
                for u in all_users:
                    uid = u[0]
                    user = get_user(uid)
                    if not user:
                        continue
                    events = get_upcoming_events(uid, days=1)
                    tasks = get_tasks(uid)
                    name = user.get("name") or "друг"
                    today_str = f"{weekdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}"

                    lines = [f"Доброе утро, <b>{name}</b>! Сегодня {today_str}."]

                    if events:
                        lines.append("\n<b>События сегодня:</b>")
                        for e in events:
                            lines.append(f"📅 {e['title']}" + (f" в {e['event_time']}" if e['event_time'] else ""))
                        if len(events) > 2:
                            lines.append("\nНасыщенный день — не забудь про перерывы.")

                    urgent_tasks = [t for t in tasks if t.get('due_date')]
                    if urgent_tasks:
                        lines.append(f"\n<b>Задач со сроком:</b> {len(urgent_tasks)}")

                    if not events and not urgent_tasks:
                        lines.append("\nСобытий нет. Если что нужно — я здесь.")

                    try:
                        await bot.send_message(uid, "\n".join(lines), parse_mode="HTML")
                    except Exception:
                        pass

            if current_hour == 20:
                tomorrow_events = get_events_to_remind()
                for event in tomorrow_events:
                    try:
                        await bot.send_message(
                            event["user_telegram_id"],
                            f"⏰ Завтра: <b>{event['title']}</b>" +
                            (f" в {event['event_time']}" if event['event_time'] else ""),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                overdue = get_overdue_tasks()
                for task in overdue:
                    try:
                        await bot.send_message(
                            task["user_telegram_id"],
                            f"⚠️ Просроченная задача: <b>{task['title']}</b>",
                            parse_mode="HTML"
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
                        lines = [f"<b>План на следующую неделю, {name}:</b>\n"]
                        for e in events:
                            lines.append(f"📅 {e['title']} — {format_date(e['event_date'])}" +
                                (f" в {e['event_time']}" if e['event_time'] else ""))
                        msg = "\n".join(lines)
                    else:
                        msg = f"На следующей неделе у тебя пока ничего нет, <b>{name}</b>."
                    try:
                        await bot.send_message(uid, msg, parse_mode="HTML")
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
    init_projects_and_tasks(lambda: __import__('psycopg2').connect(os.getenv("DATABASE_URL")))
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
