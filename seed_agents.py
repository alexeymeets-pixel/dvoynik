import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")

AGENTS = [
    {
        "telegram_id": 9000000001,
        "username": "dmitry_producer",
        "name": "Дмитрий",
        "agent_name": "Дмитрий-Агент",
        "age_range": "35–45",
        "city": "Москва",
        "occupation": "Предприниматель",
        "sphere": "Кино и медиа",
        "goal": "Найти партнёра или команду",
        "open_to_meet": "Да, всегда",
        "free_time": "Кино, театр, путешествия",
        "proud_of": "Продюсер двух полнометражных фильмов",
        "onboarding_done": 1,
    },
    {
        "telegram_id": 9000000002,
        "username": "anna_designer",
        "name": "Анна",
        "agent_name": "Анна-Агент",
        "age_range": "25–35",
        "city": "Санкт-Петербург",
        "occupation": "Фрилансер",
        "sphere": "Дизайн и визуальные коммуникации",
        "goal": "Найти клиентов",
        "open_to_meet": "Да, всегда",
        "free_time": "Фотография, выставки, йога",
        "proud_of": "Работала с крупными брендами — Яндекс, Сбер",
        "onboarding_done": 1,
    },
    {
        "telegram_id": 9000000003,
        "username": "maxim_dev",
        "name": "Максим",
        "agent_name": "Макс-Агент",
        "age_range": "25–35",
        "city": "Москва",
        "occupation": "Фрилансер",
        "sphere": "IT и разработка",
        "goal": "Найти партнёра или команду",
        "open_to_meet": "Только по делу",
        "free_time": "Хакатоны, игры, спорт",
        "proud_of": "Разработал несколько AI продуктов",
        "onboarding_done": 1,
    },
    {
        "telegram_id": 9000000004,
        "username": "olga_marketing",
        "name": "Ольга",
        "agent_name": "Ольга-Агент",
        "age_range": "35–45",
        "city": "Москва",
        "occupation": "Работаю в компании",
        "sphere": "Маркетинг и PR",
        "goal": "Просто общаться",
        "open_to_meet": "Да, всегда",
        "free_time": "Бег, подкасты, кофе с друзьями",
        "proud_of": "15 лет в маркетинге крупных брендов",
        "onboarding_done": 1,
    },
    {
        "telegram_id": 9000000005,
        "username": "ivan_investor",
        "name": "Иван",
        "agent_name": "Иван-Агент",
        "age_range": "35–45",
        "city": "Москва",
        "occupation": "Предприниматель",
        "sphere": "Инвестиции и венчур",
        "goal": "Найти партнёра или команду",
        "open_to_meet": "Только по делу",
        "free_time": "Теннис, чтение, путешествия",
        "proud_of": "Вложил в 20 стартапов, 3 из них успешно вышли",
        "onboarding_done": 1,
    },
]

def seed():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        c = conn.cursor()
        for a in AGENTS:
            c.execute("""
                INSERT INTO users (
                    telegram_id, username, name, agent_name, age_range, city,
                    occupation, sphere, goal, open_to_meet, free_time, proud_of, onboarding_done
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username=EXCLUDED.username,
                    name=EXCLUDED.name,
                    agent_name=EXCLUDED.agent_name,
                    onboarding_done=EXCLUDED.onboarding_done
            """, (
                a["telegram_id"], a["username"], a["name"], a["agent_name"],
                a["age_range"], a["city"], a["occupation"], a["sphere"],
                a["goal"], a["open_to_meet"], a["free_time"], a["proud_of"],
                a["onboarding_done"]
            ))
        conn.commit()
        print(f"Добавлено {len(AGENTS)} агентов")
    finally:
        conn.close()

if __name__ == "__main__":
    seed()
