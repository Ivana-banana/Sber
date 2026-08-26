import sqlite3

def seed(db_path: str) -> None:
    with sqlite3.connect(db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS stock(name TEXT)")
        c.execute("DELETE FROM stock")
        c.executemany("INSERT INTO stock VALUES (?)", [('amber cable',), ('brass key',), ('cobalt cable',)])

def match_names(db_path: str, phrase: str) -> list[str]:
    with sqlite3.connect(db_path) as c:
        query = f"SELECT name FROM stock WHERE name LIKE '%{phrase}%' ORDER BY name"
        return [row[0] for row in c.execute(query)]
