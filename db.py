import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

ALLOWED_FIELDS = {
    "name", "agent_name", "age_range", "city", "occupation",
    "sphere", "goal", "open_to_meet", "free_time", "proud_of", 
    "onboarding_done", "username"
}

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                telegram_id     BIGINT PRIMARY KEY,
                username        TEXT,
                name            TEXT,
                agent_name      TEXT,
                age_range       TEXT,
                city            TEXT,
                occupation      TEXT,
                sphere          TEXT,
                goal            TEXT,
                open_to_meet    TEXT,
                free_time       TEXT,
                proud_of        TEXT,
                onboarding_done INTEGER DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        ''')
        # Добавляем username если колонки нет
        c.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id          SERIAL PRIMARY KEY,
                telegram_id BIGINT,
                role        TEXT,
                content     TEXT,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS onboarding (
                telegram_id BIGINT PRIMARY KEY,
                step        INTEGER DEFAULT 0
            )
        ''')
        # Таблица друзей
        c.execute('''
            CREATE TABLE IF NOT EXISTS friends (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT,
                friend_id   BIGINT,
                status      TEXT DEFAULT 'pending',
                created_at  TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, friend_id)
            )
        ''')
        conn.commit()
        logger.info("БД инициализирована")
    finally:
        conn.close()


def get_user(telegram_id):
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_username(username):
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        username = username.lstrip('@').lower()
        c.execute("SELECT * FROM users WHERE LOWER(username) = %s", (username,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_user_field(telegram_id, field, value):
    if field not in ALLOWED_FIELDS:
        raise ValueError(f"Invalid field: {field}")
    if isinstance(value, str) and len(value) > 500:
        value = value[:500]
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING", (telegram_id,))
        c.execute(f"UPDATE users SET {field} = %s WHERE telegram_id = %s", (value, telegram_id))
        conn.commit()
    finally:
        conn.close()


def get_onboarding_step(telegram_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO onboarding (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING", (telegram_id,))
        c.execute("SELECT step FROM onboarding WHERE telegram_id = %s", (telegram_id,))
        row = c.fetchone()
        conn.commit()
        return row[0] if row else 0
    finally:
        conn.close()


def set_onboarding_step(telegram_id, step):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO onboarding (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING", (telegram_id,))
        c.execute("UPDATE onboarding SET step = %s WHERE telegram_id = %s", (step, telegram_id))
        conn.commit()
    finally:
        conn.close()


def save_message(telegram_id, role, content):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO messages (telegram_id, role, content) VALUES (%s, %s, %s)",
            (telegram_id, role, content)
        )
        conn.commit()
    finally:
        conn.close()


def get_history(telegram_id, limit=20):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT role, content FROM messages WHERE telegram_id = %s ORDER BY created_at DESC LIMIT %s",
            (telegram_id, limit)
        )
        rows = c.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    finally:
        conn.close()


def send_friend_request(from_id, to_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute('''
            INSERT INTO friends (user_id, friend_id, status)
            VALUES (%s, %s, 'pending')
            ON CONFLICT (user_id, friend_id) DO NOTHING
        ''', (from_id, to_id))
        conn.commit()
    finally:
        conn.close()


def accept_friend_request(from_id, to_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        # Принимаем запрос
        c.execute('''
            UPDATE friends SET status = 'accepted'
            WHERE user_id = %s AND friend_id = %s
        ''', (from_id, to_id))
        # Создаём обратную связь
        c.execute('''
            INSERT INTO friends (user_id, friend_id, status)
            VALUES (%s, %s, 'accepted')
            ON CONFLICT (user_id, friend_id) DO UPDATE SET status = 'accepted'
        ''', (to_id, from_id))
        conn.commit()
    finally:
        conn.close()


def get_friends(telegram_id):
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''
            SELECT u.telegram_id, u.name, u.agent_name, u.city, u.sphere, u.goal, u.username
            FROM friends f
            JOIN users u ON u.telegram_id = f.friend_id
            WHERE f.user_id = %s AND f.status = 'accepted'
        ''', (telegram_id,))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def get_pending_requests(telegram_id):
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''
            SELECT u.telegram_id, u.name, u.agent_name, u.username
            FROM friends f
            JOIN users u ON u.telegram_id = f.user_id
            WHERE f.friend_id = %s AND f.status = 'pending'
        ''', (telegram_id,))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def get_all_users():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT telegram_id, name, agent_name, city, occupation, sphere, goal
            FROM users WHERE onboarding_done = 1
        """)
        return c.fetchall()
    finally:
        conn.close()
