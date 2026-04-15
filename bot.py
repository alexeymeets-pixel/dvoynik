import os
import re
import json
import logging
import asyncio
import threading
import httpx
import urllib.parse
import zoneinfo
from datetime import datetime, timedelta
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
                add_note, get_all_users,
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

def format_date(d):
    if hasattr(d, "strftime"):
        return d.strftime("%d.%m.%Y")
    return str(d)

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

# Function calling tools
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_calendar_event",
            "description": "Записать событие в календарь пользователя",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Название события"},
                    "date": {"type": "string", "description": "Дата в формате ГГГГ-ММ-ДД"},
                    "time": {"type": "string", "description": "Время в формате ЧЧ:ММ, если известно"},
                    "description": {"type": "string", "description": "Описание или адрес"}
                },
                "required": ["title", "date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": "Записать задачу пользователя",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Название задачи"},
                    "project": {"type": "string", "description": "Название проекта если есть"},
                    "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                    "due_date": {"type": "string", "description": "Срок ГГГГ-ММ-ДД если известен"}
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": "Создать новый проект",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Поиск актуальной информации в интернете — новости, места, цены, события, расписания, любая информация которой может не быть в памяти",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Сохранить важную информацию о пользователе",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"}
                },
                "required": ["content"]
            }
        }
    }
]


def build_keyboard(options):
    if not options:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=o)] for o in options], one_time_keyboard=True, resize_keyboard=True)


def build_inline(buttons):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b[0], callback_data=b[1])] for b in buttons])


def do_search(query: str) -> str:
    try:
        result = tavily.search(query, max_results=5, include_answer=True)
        items = []
        if result.get("answer"):
            items.append(f"Ответ: {result['answer']}")
        for r in result.get("results", []):
            items.append(f"- {r['title']}: {r['content'][:300]}\n  {r.get('url','')}")
        return "\n".join(items)
    except Exception as e:
        logger.error(f"Search error: {e}")
        return "Поиск не дал результатов."


async def geocode(address: str):
    if not YANDEX_MAPS_KEY:
        return None, None
    try:
        url = f"https://geocode-maps.yandex.ru/1.x/?apikey={YANDEX_MAPS_KEY}&geocode={urllib.parse.quote(address)}&format=json"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            pos = r.json()["response"]["GeoObjectCollection"]["featureMember"][0]["GeoObject"]["Point"]["pos"]
            lon, lat = pos.split(" ")
            return float(lat), float(lon)
    except Exception as e:
        logger.error(f"Geocode error: {e}")
        return None, None


async def travel_time(flat, flon, tlat, tlon) -> str:
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{flon},{flat};{tlon},{tlat}?overview=false"
        async with httpx.AsyncClient(timeout=10) as c:
            data = (await c.get(url)).json()
            m = int(data["routes"][0]["duration"] / 60)
            return f"{m} мин" if m < 60 else f"{m//60} ч {m%60} мин"
    except Exception as e:
        logger.error(f"Travel error: {e}")
        return None


def get_taxi_link(dest: str) -> str:
    return f"https://taxi.yandex.ru/route/?end={urllib.parse.quote(dest)}"


def check_conflicts(telegram_id, event_date, event_time):
    conflicts = []
    for e in get_upcoming_events(telegram_id, days=30):
        d = e["event_date"]
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        if ds == event_date and e["event_time"] and event_time and e["event_time"][:5] == event_time[:5]:
            conflicts.append(e["title"])
    return conflicts


async def execute_tool(tool_name: str, args: dict, telegram_id: int) -> str:
    if tool_name == "add_calendar_event":
        title = args["title"]
        date = args["date"]
        time = args.get("time")
        desc = args.get("description")
        conflicts = check_conflicts(telegram_id, date, time)
        add_event(telegram_id, title, date, time, desc)
        msg = f"📅 Записал: <b>{title}</b> — {date}" + (f" в {time}" if time else "")
        if conflicts:
            msg += f"\n⚠️ Конфликт с: {', '.join(conflicts)}"
        return msg

    elif tool_name == "add_task":
        title = args["title"]
        project_name = args.get("project", "")
        priority = args.get("priority", "normal")
        due_date = args.get("due_date")
        project_id = None
        if project_name:
            p = get_project_by_name(telegram_id, project_name)
            if p:
                project_id = p["id"]
        add_task(telegram_id, title, project_id, priority, due_date)
        proj_str = f" → {project_name}" if project_name else ""
        return f"✅ Задача: <b>{title}</b>{proj_str}"

    elif tool_name == "create_project":
        name = args["name"]
        desc = args.get("description", "")
        add_project(telegram_id, name, desc)
        return f"🗂 Проект создан: <b>{name}</b>"

    elif tool_name == "search_web":
        return do_search(args["query"])

    elif tool_name == "save_note":
        add_note(telegram_id, args["content"])
        return ""

    return ""


@dp.message(CommandStart())
async def start(message: types.Message):
    telegram_id = message.from_user.id
    save_user_field(telegram_id, "username", message.from_user.username or "")
    user = get_user(telegram_id)

    for req in get_pending_requests(telegram_id):
        kb = build_inline([
            [f"Принять {req['name']}", f"accept_{req['telegram_id']}"],
            ["Отклонить", f"decline_{req['telegram_id']}"]
        ])
        await message.answer(f"👋 <b>{req['name']}</b> хочет добавить тебя в друзья.", reply_markup=kb, parse_mode="HTML")

    if user and user["onboarding_done"]:
        friends = get_friends(telegram_id)
        events = get_upcoming_events(telegram_id, days=3)
        tasks = get_tasks(telegram_id)
        agent_name = user["agent_name"] or "Двойник"

        lines = [f"С возвращением! Я <b>{agent_name}</b>.\nДрузей: {len(friends)}"]
        if events:
            lines.append("\n<b>Ближайшие события:</b>")
            for e in events:
                lines.append(f"📅 {e['title']} — {format_date(e['event_date'])}" + (f" в {e['event_time']}" if e['event_time'] else ""))
        if tasks:
            lines.append(f"\n<b>Активных задач:</b> {len(tasks)}")
        lines.append("\n<i>добавить @username · встреча с @username · мои события · мои задачи · что ты обо мне знаешь</i>")

        await message.answer("\n".join(lines), reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
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
        f"Привет! Я агент {my_user['name']} — {my_user.get('sphere','н/д')} из {my_user.get('city','н/д')}. Цель: {my_user.get('goal','н/д')}.",
        "introduction")
    try:
        await bot.send_message(from_id, f"🎉 <b>{my_user['name']}</b> принял твой запрос!", parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("decline_"))
async def decline_friend(callback: types.CallbackQuery):
    await callback.message.edit_text("Запрос отклонён.")


@dp.message(F.location)
async def handle_location(message: types.Message):
    telegram_id = message.from_user.id
    lat, lon = message.location.latitude, message.location.longitude
    save_user_field(telegram_id, "profile_notes",
        ((get_user(telegram_id).get("profile_notes") or "") + f"\nГео: {lat},{lon}")[-1000:])

    events = get_upcoming_events(telegram_id, days=1)
    if not events:
        await message.answer("Геолокацию получил. Ближайших событий нет.")
        return

    e = events[0]
    dest = e.get("description") or e["title"]
    taxi = get_taxi_link(dest)
    travel_text = ""
    depart_text = ""

    if YANDEX_MAPS_KEY:
        tlat, tlon = await geocode(dest)
        if tlat:
            t = await travel_time(lat, lon, tlat, tlon)
            if t:
                travel_text = f"\nВремя в пути: <b>{t}</b>"
                if e.get("event_time"):
                    try:
                        h, m = map(int, e["event_time"].split(":"))
                        now = now_moscow()
                        edt = now.replace(hour=h, minute=m, second=0)
                        mins = int(t.split()[0])
                        dep = edt - timedelta(minutes=mins + 15)
                        depart_text = f"\nВыехать в <b>{dep.strftime('%H:%M')}</b>"
                    except Exception:
                        pass

    await message.answer(
        f"📍 Геолокацию получил.{travel_text}{depart_text}\n\n"
        f"Ближайшее: <b>{e['title']}</b> — {format_date(e['event_date'])}" +
        (f" в {e['event_time']}" if e['event_time'] else ""),
        parse_mode="HTML"
    )
    await message.answer(f"🚕 Яндекс Такси: {taxi}")


@dp.message(F.voice)
async def handle_voice(message: types.Message):
    telegram_id = message.from_user.id
    try:
        file = await bot.get_file(message.voice.file_id)
        file_bytes = await bot.download_file(file.file_path)
        transcript = client.audio.transcriptions.create(
            model="whisper-1", file=("voice.ogg", file_bytes, "audio/ogg"), language="ru"
        )
        text = transcript.text.strip()
        if text:
            await process_agent(message, telegram_id, text)
        else:
            await message.answer("Не смог разобрать — попробуй ещё раз.")
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await message.answer("Не смог обработать голосовое.")


@dp.message(F.text)
async def handle_message(message: types.Message):
    telegram_id = message.from_user.id
    text = message.text.strip()
    if not text:
        return

    save_user_field(telegram_id, "username", message.from_user.username or "")
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

    tl = text.lower()

    # Мои события
    if any(w in tl for w in ["мои события", "мой календарь", "что у меня", "план на"]):
        events = get_upcoming_events(telegram_id, days=30)
        if not events:
            await message.answer("Пока нет событий. Расскажи о планах — я запомню.")
        else:
            now = now_moscow()
            wdays = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
            lines = [f"<b>События (сегодня {wdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}):</b>\n"]
            for i, e in enumerate(events, 1):
                lines.append(f"{i}. {e['title']} — {format_date(e['event_date'])}" + (f" в {e['event_time']}" if e['event_time'] else ""))
            await message.answer("\n".join(lines), parse_mode="HTML")
        return

    # Мои задачи
    if any(w in tl for w in ["мои задачи", "список задач", "что нужно сделать"]):
        tasks = get_tasks(telegram_id)
        projects = get_projects(telegram_id)
        if not tasks and not projects:
            await message.answer("Задач пока нет. Просто скажи что нужно сделать — я запишу.")
        else:
            lines = []
            for p in projects:
                pt = get_tasks(telegram_id, p['id'])
                if pt:
                    lines.append(f"\n🗂 <b>{p['name']}</b>")
                    for t in pt:
                        lines.append(f"  — {t['title']}" + (f" (до {format_date(t['due_date'])})" if t.get('due_date') else ""))
            standalone = [t for t in tasks if not t.get('project_id')]
            if standalone:
                lines.append("\n<b>Без проекта:</b>")
                for t in standalone:
                    lines.append(f"— {t['title']}" + (f" (до {format_date(t['due_date'])})" if t.get('due_date') else ""))
            await message.answer("\n".join(lines), parse_mode="HTML")
        return

    # Такси
    if any(w in tl for w in ["такси", "яндекс такси", "заказать такси", "вызвать такси"]):
        events = get_upcoming_events(telegram_id, days=1)
        if events:
            dest = events[0].get('description') or events[0]['title']
            await message.answer(f"🚕 Яндекс Такси до <b>{events[0]['title']}</b>:\n{get_taxi_link(dest)}", parse_mode="HTML")
        else:
            dest = re.sub(r'такси|яндекс|заказать|вызвать', '', text, flags=re.I).strip() or "Москва"
            await message.answer(f"🚕 Яндекс Такси:\n{get_taxi_link(dest)}")
        return

    # Добавить друга
    if tl.startswith("добавить") and "@" in text:
        uname = text.split("@")[1].strip().split()[0]
        target = get_user_by_username(uname)
        if not target:
            await message.answer(f"@{uname} не зарегистрирован в сети.")
            return
        if target["telegram_id"] == telegram_id:
            await message.answer("Это твой аккаунт.")
            return
        send_friend_request(telegram_id, target["telegram_id"])
        my = get_user(telegram_id)
        kb = build_inline([["Принять", f"accept_{telegram_id}"], ["Отклонить", f"decline_{telegram_id}"]])
        try:
            await bot.send_message(target["telegram_id"],
                f"👋 <b>{my['name']}</b> хочет добавить тебя в друзья.", reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        await message.answer("Запрос отправлен.")
        return

    # Встреча
    if any(w in tl for w in ["встреча с", "встретиться с"]) and "@" in text:
        uname = text.split("@")[1].strip().split()[0]
        target = get_user_by_username(uname)
        if target:
            await organize_meeting(message, telegram_id, target)
            return

    await process_agent(message, telegram_id, text)


async def organize_meeting(message: types.Message, user_id: int, target: dict):
    user = get_user(user_id)
    city = user.get("city") or "Москва"
    target_name = target["name"] or "друг"

    send_agent_message(user_id, target["telegram_id"],
        f"Привет! Я агент {user['name']}. Хочет встретиться. Когда удобно?", "meeting_request")
    try:
        await bot.send_message(target["telegram_id"],
            f"📅 <b>{user['name']}</b> хочет встретиться. Когда тебе удобно?", parse_mode="HTML")
    except Exception:
        pass

    places = do_search(f"кофейня для встречи {city} адрес рейтинг")
    reply = f"Отправил запрос {target_name}. Когда тебе удобно?"
    if places:
        reply += f"\n\n<b>Варианты мест в {city}:</b>\n{places}"
    await message.answer(reply, parse_mode="HTML")


async def process_agent(message: types.Message, telegram_id: int, text: str):
    user = get_user(telegram_id)
    history = get_history(telegram_id)
    friends = get_friends(telegram_id)
    agent_msgs = get_unread_agent_messages(telegram_id)
    events = get_upcoming_events(telegram_id, days=7)
    tasks = get_tasks(telegram_id)
    projects = get_projects(telegram_id)

    now = now_moscow()
    wdays = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]

    friends_info = "\n".join([f"- {f['name']} ({f.get('sphere','н/д')}, {f.get('city','н/д')})" for f in friends]) or "Нет"
    events_info = "\n".join([f"- {e['title']} — {format_date(e['event_date'])}" + (f" в {e['event_time']}" if e['event_time'] else "") for e in events]) or "Нет"

    tasks_lines = []
    for p in projects:
        pt = get_tasks(telegram_id, p['id'])
        if pt:
            tasks_lines.append(f"Проект {p['name']}:")
            for t in pt:
                tasks_lines.append(f"  [{t['id']}] {t['title']}")
    standalone = [t for t in tasks if not t.get('project_id')]
    for t in standalone:
        tasks_lines.append(f"[{t['id']}] {t['title']}")
    tasks_info = "\n".join(tasks_lines) or "Нет"

    agent_info = "\n".join([f"- От {m['from_name']}: {m['message']}" for m in agent_msgs]) or "Нет"

    system = f"""Ты личный AI-агент по имени {user.get('agent_name','Двойник')}. Цифровой двойник {user.get('name','друга')}.

Сейчас: {wdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}, {now.strftime('%H:%M')} по Москве.

Пиши как живой человек — тепло, по-человечески, иногда с юмором. Без фраз "конечно!", "дай знать!", "сообщи!". Никогда не заканчивай дежурной фразой.

Используй HTML: <b>жирный</b>, <i>курсив</i>. Без звёздочек и решёток.

Профиль: {user.get('city','н/д')} | {user.get('sphere','н/д')} | {user.get('occupation','н/д')} | {user.get('goal','н/д')}
Места: {user.get('preferred_places','не указаны')} | Время встреч: {user.get('preferred_time','не указано')}
Заметки: {user.get('profile_notes','нет')}

Друзья: {friends_info}
События: {events_info}
Задачи: {tasks_info}
От агентов: {agent_info}

Используй инструменты когда нужно:
- add_calendar_event — когда упоминается событие с датой
- add_task — когда нужно что-то сделать или запомнить задачу
- create_project — когда упоминается новый проект
- search_web — когда нужна актуальная информация из интернета
- save_note — когда узнаёшь что-то важное о человеке

Если спрашивают "что ты обо мне знаешь" — пиши живой портрет несколькими абзацами без списков."""

    date_prefix = f"[{wdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}, {now.strftime('%H:%M')}]\n"
    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": date_prefix + text}]

    tool_results = []
    final_reply = ""

    try:
        # Первый вызов — GPT решает нужны ли инструменты
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=800,
            temperature=0.8,
        )

        msg = response.choices[0].message

        # Если GPT вызвал инструменты
        if msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]})

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result = await execute_tool(tc.function.name, args, telegram_id)
                if result:
                    tool_results.append(result)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result or "выполнено"})

            # Второй вызов — финальный ответ с учётом результатов
            response2 = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                max_tokens=600,
                temperature=0.8,
            )
            final_reply = response2.choices[0].message.content.strip()
        else:
            final_reply = msg.content.strip() if msg.content else ""

    except APIError as e:
        logger.error(f"OpenAI error: {e}")
        final_reply = "Что-то пошло не так, попробую ещё раз."

    save_message(telegram_id, "user", text)
    save_message(telegram_id, "assistant", final_reply)

    if final_reply:
        await message.answer(final_reply, parse_mode="HTML")
    for r in tool_results:
        if r:
            await message.answer(r, parse_mode="HTML")


async def reminder_loop():
    while True:
        try:
            now = now_moscow()
            h = now.hour
            wday = now.weekday()
            wdays = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]

            if h == 9:
                for u in get_all_users():
                    uid = u[0]
                    user = get_user(uid)
                    if not user:
                        continue
                    events = get_upcoming_events(uid, days=1)
                    tasks = get_tasks(uid)
                    name = user.get("name","друг")
                    lines = [f"Доброе утро, <b>{name}</b>! Сегодня {wdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}."]
                    if events:
                        lines.append("\n<b>Сегодня:</b>")
                        for e in events:
                            lines.append(f"📅 {e['title']}" + (f" в {e['event_time']}" if e['event_time'] else ""))
                    urgent = [t for t in tasks if t.get('due_date')]
                    if urgent:
                        lines.append(f"\n<b>Задач со сроком:</b> {len(urgent)}")
                    if not events and not urgent:
                        lines.append("\nСобытий нет. Если что нужно — я здесь.")
                    try:
                        await bot.send_message(uid, "\n".join(lines), parse_mode="HTML")
                    except Exception:
                        pass

            if h == 20:
                for event in get_events_to_remind():
                    try:
                        await bot.send_message(
                            event["user_telegram_id"],
                            f"⏰ Завтра: <b>{event['title']}</b>" + (f" в {event['event_time']}" if event['event_time'] else ""),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                for task in get_overdue_tasks():
                    try:
                        await bot.send_message(
                            task["user_telegram_id"],
                            f"⚠️ Просроченная задача: <b>{task['title']}</b>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            if wday == 6 and h == 19:
                for u in get_all_users():
                    uid = u[0]
                    user = get_user(uid)
                    if not user:
                        continue
                    events = get_upcoming_events(uid, days=7)
                    name = user.get("name","друг")
                    if events:
                        lines = [f"<b>План на неделю, {name}:</b>\n"]
                        for e in events:
                            lines.append(f"📅 {e['title']} — {format_date(e['event_date'])}" + (f" в {e['event_time']}" if e['event_time'] else ""))
                        msg = "\n".join(lines)
                    else:
                        msg = f"На следующей неделе у тебя пока ничего нет, <b>{name}</b>."
                    try:
                        await bot.send_message(uid, msg, parse_mode="HTML")
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Reminder error: {e}")
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
    from db import get_conn
    init_projects_and_tasks(get_conn)
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Бот запущен...")
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
