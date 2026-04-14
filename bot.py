import os
import logging
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from db import (init_db, get_user, save_user_field, get_onboarding_step,
                set_onboarding_step, save_message, get_history, get_all_users)

logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

client = OpenAI(api_key=OPENAI_API_KEY)

# Шаги онбординга
ONBOARDING_STEPS = [
    ("name",         "Привет! Я твой личный агент-двойник 👤\n\nКак тебя зовут?", None),
    ("agent_name",   "Отлично! Теперь придумай имя своему агенту — он будет представляться этим именем в сети:", None),
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

Твоя задача:
- Общаться как живой человек — тепло, естественно, без формальностей
- Задавать один вопрос за раз чтобы лучше узнать пользователя
- Запоминать всё что говорит пользователь
- Помогать договариваться о встречах с другими пользователями сети
- Иногда писать первым если есть повод из разговора

Говори только по-русски. Ответы короткие — 2-3 предложения максимум."""


def build_keyboard(options):
    if not options:
        return ReplyKeyboardRemove()
    keyboard = [[opt] for opt in options]
    return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)

    if user and user["onboarding_done"]:
        agent_name = user["agent_name"] or "Двойник"
        await update.message.reply_text(
            f"С возвращением! Я {agent_name}, твой агент. Чем могу помочь?",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    # Начинаем онбординг
    set_onboarding_step(telegram_id, 0)
    step_data = ONBOARDING_STEPS[0]
    keyboard = build_keyboard(step_data[2])
    await update.message.reply_text(step_data[1], reply_markup=keyboard)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    text = update.message.text.strip()
    user = get_user(telegram_id)

    # Онбординг не завершён
    if not user or not user["onboarding_done"]:
        step = get_onboarding_step(telegram_id)

        if step < len(ONBOARDING_STEPS):
            field, _, _ = ONBOARDING_STEPS[step]
            save_user_field(telegram_id, field, text)
            next_step = step + 1

            if next_step < len(ONBOARDING_STEPS):
                set_onboarding_step(telegram_id, next_step)
                next_data = ONBOARDING_STEPS[next_step]
                keyboard = build_keyboard(next_data[2])
                await update.message.reply_text(next_data[1], reply_markup=keyboard)
            else:
                # Онбординг завершён
                save_user_field(telegram_id, "onboarding_done", 1)
                user = get_user(telegram_id)
                agent_name = user["agent_name"] or "Двойник"
                await update.message.reply_text(
                    f"Отлично, {user['name']}! 🎉\n\nЯ {agent_name} — твой личный агент. "
                    f"Буду рядом, помогу находить нужных людей и решать задачи.\n\n"
                    f"Как прошёл твой день?",
                    reply_markup=ReplyKeyboardRemove()
                )
        return

    # Обычный разговор с агентом
    user = get_user(telegram_id)
    history = get_history(telegram_id)

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
    )

    messages = history + [{"role": "user", "content": text}]

    # Проверяем есть ли другие пользователи в сети
    all_users = get_all_users()
    other_users = [u for u in all_users if u[0] != telegram_id]

    if other_users and ("встреч" in text.lower() or "познаком" in text.lower() or "найди" in text.lower()):
        network_info = "\n\nСейчас в сети есть люди:\n"
        for u in other_users[:5]:
            network_info += f"- {u[1]} ({u[5]}, {u[2]}), город: {u[3]}, цель: {u[6]}\n"
        system += network_info

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system}] + messages,
        max_tokens=300,
        temperature=0.85,
    )

    reply = response.choices[0].message.content.strip()

    save_message(telegram_id, "user", text)
    save_message(telegram_id, "assistant", reply)

    await update.message.reply_text(reply)


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
