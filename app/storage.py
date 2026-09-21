"""Single-replica encrypted session store. The key must survive redeploys."""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory: str, key: str | None = None):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        if not key:
            keyfile = root / "encryption.key"
            if not keyfile.exists():
                fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            key = keyfile.read_text().strip()
        self.cipher = Fernet(key.encode())
        self.path = root / "sessions.sqlite3"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data BLOB NOT NULL, expires REAL NOT NULL)")
        os.chmod(self.path, 0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def digest(sid):
        return hashlib.sha256(sid.encode()).hexdigest()

    def create(self):
        sid = secrets.token_urlsafe(32)
        data = {"csrf": secrets.token_urlsafe(32), "device": secrets.token_hex(16), "created": time.time()}
        self.save(sid, data)
        return sid, data

    def load(self, sid):
        if not sid or len(sid) != 43:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM sessions WHERE id=? AND expires>?", (self.digest(sid), time.time())).fetchone()
        if row is None:
            return None
        return json.loads(self.cipher.decrypt(row[0]))

    def save(self, sid, data):
        raw = self.cipher.encrypt(json.dumps(data, ensure_ascii=False).encode())
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)", (self.digest(sid), raw, time.time() + 30 * 86400))
            conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))

    def delete(self, sid):
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id=?", (self.digest(sid),))
