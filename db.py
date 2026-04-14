import sqlite3

DB_PATH = "dvoynik.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id     INTEGER PRIMARY KEY,
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
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            role        TEXT,
            content     TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS onboarding (
            telegram_id INTEGER PRIMARY KEY,
            step        INTEGER DEFAULT 0
        )
    ''')

    conn.commit()
    conn.close()


def get_user(telegram_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    conn.close()
    if row:
        keys = ["telegram_id","name","agent_name","age_range","city",
                "occupation","sphere","goal","open_to_meet","free_time",
                "proud_of","onboarding_done","created_at"]
        return dict(zip(keys, row))
    return None


def save_user_field(telegram_id, field, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))
    c.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, telegram_id))
    conn.commit()
    conn.close()


def get_onboarding_step(telegram_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO onboarding (telegram_id) VALUES (?)", (telegram_id,))
    c.execute("SELECT step FROM onboarding WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 0


def set_onboarding_step(telegram_id, step):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO onboarding (telegram_id) VALUES (?)", (telegram_id,))
    c.execute("UPDATE onboarding SET step = ? WHERE telegram_id = ?", (step, telegram_id))
    conn.commit()
    conn.close()


def save_message(telegram_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (telegram_id, role, content) VALUES (?, ?, ?)",
        (telegram_id, role, content)
    )
    conn.commit()
    conn.close()


def get_history(telegram_id, limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM messages WHERE telegram_id = ? ORDER BY created_at DESC LIMIT ?",
        (telegram_id, limit)
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT telegram_id, name, agent_name, city, occupation, sphere, goal FROM users WHERE onboarding_done = 1")
    rows = c.fetchall()
    conn.close()
    return rows
