import sqlite3

def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0,
            referrer_id INTEGER DEFAULT NULL, last_bonus TEXT DEFAULT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            channel_username TEXT, req_count INTEGER,
            done_count INTEGER DEFAULT 0, channel_msg_id INTEGER DEFAULT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS completed_subs (
            user_id INTEGER, channel_username TEXT,
            PRIMARY KEY (user_id, channel_username)
        )
    ''')
    # Indekslar (Qidiruvni tezlashtirish uchun)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subs ON completed_subs(user_id, channel_username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ref ON users(referrer_id)")
    conn.commit()
    conn.close()

def get_or_create_user(user_id, referrer_id=None):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        valid_ref = referrer_id if (referrer_id and referrer_id != user_id) else None
        cursor.execute("INSERT INTO users (user_id, balance, referrer_id) VALUES (?, 0.0, ?)", (user_id, valid_ref))
        conn.commit()
        if valid_ref:
            cursor.execute("UPDATE users SET balance = balance + 0.02 WHERE user_id = ?", (valid_ref,))
            conn.commit()
        balance = 0.0
    else:
        balance = row[0]
    conn.close()
    return balance

def update_balance(user_id, amount):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()