import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(**name**)

DATABASE_URL = os.getenv(“DATABASE_URL”)

ALLOWED_FIELDS = {
“name”, “agent_name”, “age_range”, “city”, “occupation”,
“sphere”, “goal”, “open_to_meet”, “free_time”, “proud_of”, “onboarding_done”
}

def get_conn():
return psycopg2.connect(DATABASE_URL)

def init_db():
conn = get_conn()
try:
c = conn.cursor()
c.execute(’’’
CREATE TABLE IF NOT EXISTS users (
telegram_id     BIGINT PRIMARY KEY,
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
‘’’)
c.execute(’’’
CREATE TABLE IF NOT EXISTS messages (
id          SERIAL PRIMARY KEY,
telegram_id BIGINT,
role        TEXT,
content     TEXT,
created_at  TIMESTAMP DEFAULT NOW()
)
‘’’)
c.execute(’’’
CREATE TABLE IF NOT EXISTS onboarding (
telegram_id BIGINT PRIMARY KEY,
step        INTEGER DEFAULT 0
)
‘’’)
conn.commit()
logger.info(“БД инициализирована”)
finally:
conn.close()

def get_user(telegram_id):
conn = get_conn()
try:
c = conn.cursor(cursor_factory=RealDictCursor)
c.execute(“SELECT * FROM users WHERE telegram_id = %s”, (telegram_id,))
row = c.fetchone()
return dict(row) if row else None
finally:
conn.close()

def save_user_field(telegram_id, field, value):
if field not in ALLOWED_FIELDS:
raise ValueError(f”Invalid field: {field}”)
if isinstance(value, str) and len(value) > 500:
value = value[:500]
conn = get_conn()
try:
c = conn.cursor()
c.execute(“INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING”, (telegram_id,))
c.execute(f”UPDATE users SET {field} = %s WHERE telegram_id = %s”, (value, telegram_id))
conn.commit()
finally:
conn.close()

def get_onboarding_step(telegram_id):
conn = get_conn()
try:
c = conn.cursor()
c.execute(“INSERT INTO onboarding (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING”, (telegram_id,))
c.execute(“SELECT step FROM onboarding WHERE telegram_id = %s”, (telegram_id,))
row = c.fetchone()
conn.commit()
return row[0] if row else 0
finally:
conn.close()

def set_onboarding_step(telegram_id, step):
conn = get_conn()
try:
c = conn.cursor()
c.execute(“INSERT INTO onboarding (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING”, (telegram_id,))
c.execute(“UPDATE onboarding SET step = %s WHERE telegram_id = %s”, (step, telegram_id))
conn.commit()
finally:
conn.close()

def save_message(telegram_id, role, content):
conn = get_conn()
try:
c = conn.cursor()
c.execute(
“INSERT INTO messages (telegram_id, role, content) VALUES (%s, %s, %s)”,
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
“SELECT role, content FROM messages WHERE telegram_id = %s ORDER BY created_at DESC LIMIT %s”,
(telegram_id, limit)
)
rows = c.fetchall()
return [{“role”: r[0], “content”: r[1]} for r in reversed(rows)]
finally:
conn.close()

def get_all_users():
conn = get_conn()
try:
c = conn.cursor()
c.execute(”””
SELECT telegram_id, name, agent_name, city, occupation, sphere, goal
FROM users WHERE onboarding_done = 1
“””)
return c.fetchall()
finally:
conn.close()
