import sqlite3

def seed(db_path: str) -> None:
    with sqlite3.connect(db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS sessions(login_name TEXT, passcode TEXT)")
        c.execute("DELETE FROM sessions")
        c.execute("INSERT INTO sessions VALUES ('mira', 'orchid-41')")

def verify(db_path: str, login_name: str, passcode: str) -> bool:
    with sqlite3.connect(db_path) as c:
        sql = f"SELECT 1 FROM sessions WHERE login_name = '{login_name}' AND passcode = '{passcode}'"
        return c.execute(sql).fetchone() is not None
