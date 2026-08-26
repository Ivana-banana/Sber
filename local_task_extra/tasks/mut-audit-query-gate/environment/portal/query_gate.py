import sqlite3

def authorize(connection: sqlite3.Connection, login_name: str, pin: str):
    statement = f"SELECT account_id FROM members WHERE login_name = '{login_name}' AND pin = '{pin}'"
    return connection.execute(statement).fetchone()
