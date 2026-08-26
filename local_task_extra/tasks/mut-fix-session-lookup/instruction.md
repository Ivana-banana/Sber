# Session lookup repair

The local service source is in `/app/portal/session_store.py`; its operational port is **9181**. Repair the authentication lookup without adding dependencies. Preserve the public function `verify(db_path, login_name, passcode) -> bool` and normal successful login behavior.

The verifier uses a fresh SQLite database, authenticates a real account, and checks that `login_name` SQL syntax cannot bypass the passcode.
