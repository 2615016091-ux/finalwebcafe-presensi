#!/usr/bin/env python3
"""Ruang Presensi: pure Python HTTP server with SQLite storage."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "presensi.sqlite"
PUBLIC_DIR = ROOT / "public"
MAX_BODY = 2_200_000
RATE_LIMIT: dict[str, dict[str, float | int]] = {}
RATE_LOCK = threading.Lock()
TIMEZONES = {
    "Asia/Jakarta": timezone(timedelta(hours=7)),
    "Asia/Makassar": timezone(timedelta(hours=8)),
    "Asia/Jayapura": timezone(timedelta(hours=9)),
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT NOT NULL,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','employee')),active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT NOT NULL,lat REAL,lng REAL,radius INTEGER DEFAULT 100,max_accuracy INTEGER DEFAULT 75,timezone TEXT DEFAULT 'Asia/Jakarta');
INSERT OR IGNORE INTO settings(id,name) VALUES(1,'Basa Basi Coffee');
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id),csrf TEXT,expires INTEGER);
CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),kind TEXT CHECK(kind IN ('in','out')),created_at TEXT NOT NULL,work_date TEXT NOT NULL,lat REAL,lng REAL,accuracy REAL,distance INTEGER,photo BLOB NOT NULL,status TEXT DEFAULT 'pending',review_note TEXT DEFAULT '',clarification TEXT DEFAULT '',reviewer INTEGER REFERENCES users(id),reviewed_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_event ON attendance(user_id,work_date,kind);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor INTEGER,action TEXT,detail TEXT,created_at TEXT);
"""


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64)
    return f"{salt}:{key.hex()}"


def password_verify(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split(":", 1)
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rad = math.pi / 180
    hav = math.sin((lat2 - lat1) * rad / 2) ** 2 + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin((lng2 - lng1) * rad / 2) ** 2
    return 6_371_000 * 2 * math.atan2(math.sqrt(hav), math.sqrt(1 - hav))


def validate_location(payload: dict, setting: sqlite3.Row) -> int:
    values = [payload.get("lat"), payload.get("lng"), payload.get("accuracy")]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
        raise AppError("Koordinat GPS tidak valid.")
    lat, lng, accuracy = values
    if abs(lat) > 90 or abs(lng) > 180 or accuracy < 0:
        raise AppError("Koordinat GPS tidak valid.")
    if accuracy > setting["max_accuracy"]:
        raise AppError(f"GPS belum akurat (±{round(accuracy)} m; batas ±{setting['max_accuracy']} m). Pindah ke area terbuka dan perbarui lokasi.")
    meters = round(distance_meters(lat, lng, setting["lat"], setting["lng"]))
    if meters > setting["radius"]:
        raise AppError(f"Anda berada {meters} m dari kafe, di luar radius {setting['radius']} m. Ulangi presensi di lokasi kafe.")
    return meters


def text_value(value, minimum: int, maximum: int, label: str) -> str:
    if not isinstance(value, str):
        raise AppError(f"{label} harus {minimum}–{maximum} karakter.")
    value = value.strip()
    if len(value) < minimum or len(value) > maximum:
        raise AppError(f"{label} harus {minimum}–{maximum} karakter.")
    return value


def create_admin() -> None:
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or len(password) < 12:
        raise AppError("Isi ADMIN_EMAIL dan ADMIN_PASSWORD (minimal 12 karakter).")
    with connect() as con:
        if con.execute("SELECT id FROM users WHERE role='admin'").fetchone():
            raise AppError("Admin sudah ada.")
        con.execute("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)", ("PJ Cabang", email, password_hash(password), "admin"))


class Handler(BaseHTTPRequestHandler):
    server_version = "RuangPresensi/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(self), geolocation=(self), microphone=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        super().end_headers()

    def send_json(self, value, status: int = 200, cookie: str | None = None) -> None:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def send_bytes(self, raw: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store" if content_type == "image/jpeg" else "public, max-age=300")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise AppError("Ukuran permintaan tidak valid.") from exc
        if size > MAX_BODY:
            raise AppError("Foto terlalu besar.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            value = json.loads(self.rfile.read(size) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AppError("JSON tidak valid.") from exc
        if not isinstance(value, dict):
            raise AppError("JSON tidak valid.")
        return value

    def cookie_token(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie.get("session").value if cookie.get("session") else ""

    def session(self, con: sqlite3.Connection) -> tuple[str, sqlite3.Row]:
        token = self.cookie_token()
        row = con.execute("SELECT s.*,u.name,u.email,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE token=? AND expires>? AND u.active=1", (token, int(time.time() * 1000))).fetchone()
        if not row:
            raise AppError("Silakan masuk terlebih dahulu.", HTTPStatus.UNAUTHORIZED)
        if self.command != "GET" and self.headers.get("X-CSRF-Token") != row["csrf"]:
            raise AppError("Sesi tidak valid. Muat ulang halaman.", HTTPStatus.FORBIDDEN)
        return token, row

    @staticmethod
    def require_admin(session: sqlite3.Row) -> None:
        if session["role"] != "admin":
            raise AppError("Akses khusus PJ cabang.", HTTPStatus.FORBIDDEN)

    @staticmethod
    def audit(con: sqlite3.Connection, actor: int, action: str, detail: dict) -> None:
        con.execute("INSERT INTO audit(actor,action,detail,created_at) VALUES(?,?,?,?)", (actor, action, json.dumps(detail, ensure_ascii=False), iso_now()))

    def do_GET(self) -> None:
        self.dispatch()

    def do_POST(self) -> None:
        self.dispatch()

    def do_PUT(self) -> None:
        self.dispatch()

    def do_PATCH(self) -> None:
        self.dispatch()

    def dispatch(self) -> None:
        try:
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                self.serve_static(path)
                return
            with connect() as con:
                self.handle_api(con, path)
        except AppError as exc:
            self.send_json({"error": str(exc)}, int(exc.status))
        except sqlite3.IntegrityError as exc:
            message = "Data sudah terdaftar." if "UNIQUE constraint" in str(exc) else "Data melanggar aturan database."
            self.send_json({"error": message}, HTTPStatus.CONFLICT)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            print("Kesalahan server:", file=sys.stderr)
            import traceback
            traceback.print_exc()
            try:
                self.send_json({"error": "Terjadi kesalahan server."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def serve_static(self, path: str) -> None:
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.command != "GET" or path not in files:
            raise AppError("Halaman tidak ditemukan.", HTTPStatus.NOT_FOUND)
        filename, content_type = files[path]
        self.send_bytes((PUBLIC_DIR / filename).read_bytes(), content_type)

    def handle_api(self, con: sqlite3.Connection, path: str) -> None:
        if path == "/api/login" and self.command == "POST":
            self.login(con)
            return
        token, session = self.session(con)
        if path == "/api/me" and self.command == "GET":
            setting = dict(con.execute("SELECT * FROM settings WHERE id=1").fetchone())
            user = {"id": session["user_id"], "name": session["name"], "email": session["email"], "role": session["role"]}
            self.send_json({"user": user, "csrf": session["csrf"], "settings": setting})
            return
        if path == "/api/logout" and self.command == "POST":
            con.execute("DELETE FROM sessions WHERE token=?", (token,))
            secure = "; Secure" if os.environ.get("NODE_ENV") == "production" or os.environ.get("APP_ENV") == "production" else ""
            self.send_json({"ok": True}, cookie=f"session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{secure}")
            return
        if path == "/api/settings" and self.command == "PUT":
            self.update_settings(con, session)
            return
        if path == "/api/users" and self.command == "GET":
            self.require_admin(session)
            self.send_json([dict(row) for row in con.execute("SELECT id,name,email,role,active FROM users ORDER BY name")])
            return
        if path == "/api/users" and self.command == "POST":
            self.create_user(con, session)
            return
        user_match = re.fullmatch(r"/api/users/(\d+)", path)
        if user_match and self.command == "PATCH":
            self.update_user(con, session, int(user_match.group(1)))
            return
        if path == "/api/password" and self.command == "POST":
            self.change_password(con, session, token)
            return
        if path == "/api/attendance" and self.command == "GET":
            self.list_attendance(con, session)
            return
        if path == "/api/attendance" and self.command == "POST":
            self.create_attendance(con, session)
            return
        match = re.fullmatch(r"/api/attendance/(\d+)/(photo|review|clarify)", path)
        if match:
            self.attendance_action(con, session, int(match.group(1)), match.group(2))
            return
        raise AppError("Endpoint tidak ditemukan.", HTTPStatus.NOT_FOUND)

    def login(self, con: sqlite3.Connection) -> None:
        payload = self.read_json()
        ip = self.client_address[0]
        now = time.time()
        with RATE_LOCK:
            limit = RATE_LIMIT.get(ip)
            if not limit or limit["until"] < now:
                limit = {"count": 0, "until": now + 900}
                RATE_LIMIT[ip] = limit
            limit["count"] += 1
            if limit["count"] > 25:
                raise AppError("Terlalu banyak percobaan. Coba lagi dalam 15 menit.", HTTPStatus.TOO_MANY_REQUESTS)
        email = str(payload.get("email", "")).strip().lower()
        user = con.execute("SELECT * FROM users WHERE email=? AND active=1", (email,)).fetchone()
        password = payload.get("password")
        if not user or not isinstance(password, str) or not password_verify(password, user["password"]):
            raise AppError("Email atau kata sandi salah.", HTTPStatus.UNAUTHORIZED)
        with RATE_LOCK:
            RATE_LIMIT.pop(ip, None)
        token, csrf = secrets.token_hex(32), secrets.token_hex(24)
        now_ms = int(time.time() * 1000)
        con.execute("DELETE FROM sessions WHERE expires<?", (now_ms,))
        con.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token, user["id"], csrf, now_ms + 43_200_000))
        secure = "; Secure" if os.environ.get("NODE_ENV") == "production" or os.environ.get("APP_ENV") == "production" else ""
        self.send_json({"ok": True}, cookie=f"session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200{secure}")

    def update_settings(self, con: sqlite3.Connection, session: sqlite3.Row) -> None:
        self.require_admin(session)
        payload = self.read_json()
        name = text_value(payload.get("name"), 2, 80, "Nama kafe")
        nums = [payload.get(k) for k in ("lat", "lng", "radius", "max_accuracy")]
        valid_nums = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in nums)
        if not valid_nums or abs(nums[0]) > 90 or abs(nums[1]) > 180 or not 10 <= nums[2] <= 1000 or not 5 <= nums[3] <= 200:
            raise AppError("Lokasi/radius tidak valid (radius 10–1000 m, akurasi 5–200 m).")
        zone = payload.get("timezone")
        if zone not in TIMEZONES:
            raise AppError("Zona waktu tidak valid.")
        con.execute("UPDATE settings SET name=?,lat=?,lng=?,radius=?,max_accuracy=?,timezone=? WHERE id=1", (name, *nums, zone))
        self.audit(con, session["user_id"], "settings", payload)
        self.send_json({"ok": True})

    def create_user(self, con: sqlite3.Connection, session: sqlite3.Row) -> None:
        self.require_admin(session)
        payload = self.read_json()
        name = text_value(payload.get("name"), 2, 80, "Nama")
        email = text_value(payload.get("email"), 5, 120, "Email").lower()
        password = text_value(payload.get("password"), 12, 128, "Kata sandi")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise AppError("Email tidak valid.")
        if payload.get("role") not in ("admin", "employee"):
            raise AppError("Peran tidak valid.")
        con.execute("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)", (name, email, password_hash(password), payload["role"]))
        self.audit(con, session["user_id"], "create_user", {"email": email})
        self.send_json({"ok": True}, HTTPStatus.CREATED)

    def update_user(self, con: sqlite3.Connection, session: sqlite3.Row, user_id: int) -> None:
        self.require_admin(session)
        payload = self.read_json()
        if user_id == session["user_id"]:
            raise AppError("Tidak dapat menonaktifkan akun sendiri.")
        if not con.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            raise AppError("Akun tidak ditemukan.", HTTPStatus.NOT_FOUND)
        if not isinstance(payload.get("active"), bool):
            raise AppError("Status tidak valid.")
        active = 1 if payload["active"] else 0
        con.execute("UPDATE users SET active=? WHERE id=?", (active, user_id))
        con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        self.audit(con, session["user_id"], "active_user", {"id": user_id, "active": payload["active"]})
        self.send_json({"ok": True})

    def change_password(self, con: sqlite3.Connection, session: sqlite3.Row, token: str) -> None:
        payload = self.read_json()
        user = con.execute("SELECT password FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not isinstance(payload.get("current"), str) or not password_verify(payload["current"], user["password"]):
            raise AppError("Kata sandi lama salah.")
        password = text_value(payload.get("password"), 12, 128, "Kata sandi")
        con.execute("UPDATE users SET password=? WHERE id=?", (password_hash(password), session["user_id"]))
        con.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (session["user_id"], token))
        self.send_json({"ok": True})

    def list_attendance(self, con: sqlite3.Connection, session: sqlite3.Row) -> None:
        fields = "a.id,a.user_id,u.name,a.kind,a.created_at,a.work_date,a.lat,a.lng,a.accuracy,a.distance,a.status,a.review_note,a.clarification,a.reviewed_at"
        sql = f"SELECT {fields} FROM attendance a JOIN users u ON u.id=a.user_id"
        params = ()
        if session["role"] != "admin":
            sql += " WHERE a.user_id=?"
            params = (session["user_id"],)
        rows = con.execute(sql + " ORDER BY a.id DESC LIMIT 2000", params).fetchall()
        self.send_json([dict(row) for row in rows])

    def create_attendance(self, con: sqlite3.Connection, session: sqlite3.Row) -> None:
        payload = self.read_json()
        setting = con.execute("SELECT * FROM settings WHERE id=1").fetchone()
        if setting["lat"] is None or setting["lng"] is None:
            raise AppError("PJ cabang belum mengatur titik lokasi kafe.")
        if payload.get("kind") not in ("in", "out"):
            raise AppError("Jenis presensi tidak valid.")
        meters = validate_location(payload, setting)
        photo_data = payload.get("photo")
        match = re.fullmatch(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", photo_data or "") if isinstance(photo_data, str) else None
        if not match:
            raise AppError("Selfie JPEG wajib dilampirkan.")
        try:
            photo = base64.b64decode(match.group(1), validate=True)
        except ValueError as exc:
            raise AppError("Foto JPEG tidak valid atau terlalu besar.") from exc
        if len(photo) < 1000 or len(photo) > 1_500_000 or photo[:2] != b"\xff\xd8" or photo[-2:] != b"\xff\xd9":
            raise AppError("Foto JPEG tidak valid atau terlalu besar.")
        now = datetime.now(timezone.utc)
        today = now.astimezone(TIMEZONES[setting["timezone"]]).date().isoformat()
        last = con.execute("SELECT * FROM attendance WHERE user_id=? ORDER BY id DESC LIMIT 1", (session["user_id"],)).fetchone()
        work_date = today
        if payload["kind"] == "in" and last and last["kind"] == "in":
            raise AppError("Anda belum presensi pulang untuk shift sebelumnya.")
        if payload["kind"] == "out":
            if not last or last["kind"] != "in":
                raise AppError("Lakukan presensi masuk terlebih dahulu.")
            work_date = last["work_date"]
        if con.execute("SELECT id FROM attendance WHERE user_id=? AND work_date=? AND kind=?", (session["user_id"], work_date, payload["kind"])).fetchone():
            raise AppError("Presensi ini sudah tercatat untuk tanggal kerja tersebut.", HTTPStatus.CONFLICT)
        cursor = con.execute("INSERT INTO attendance(user_id,kind,created_at,work_date,lat,lng,accuracy,distance,photo) VALUES(?,?,?,?,?,?,?,?,?)", (session["user_id"], payload["kind"], iso_now(), work_date, payload["lat"], payload["lng"], payload["accuracy"], meters, photo))
        self.audit(con, session["user_id"], "attendance", {"id": cursor.lastrowid})
        self.send_json({"ok": True}, HTTPStatus.CREATED)

    def attendance_action(self, con: sqlite3.Connection, session: sqlite3.Row, attendance_id: int, action: str) -> None:
        row = con.execute("SELECT * FROM attendance WHERE id=?", (attendance_id,)).fetchone()
        if not row:
            raise AppError("Data tidak ditemukan.", HTTPStatus.NOT_FOUND)
        if session["role"] != "admin" and row["user_id"] != session["user_id"]:
            raise AppError("Akses ditolak.", HTTPStatus.FORBIDDEN)
        if action == "photo" and self.command == "GET":
            self.send_bytes(row["photo"], "image/jpeg")
            return
        if action == "review" and self.command == "POST":
            self.require_admin(session)
            payload = self.read_json()
            if payload.get("status") not in ("approved", "flagged"):
                raise AppError("Status tidak valid.")
            note = text_value(payload.get("note", ""), 5 if payload["status"] == "flagged" else 0, 1000, "Catatan")
            con.execute("UPDATE attendance SET status=?,review_note=?,reviewer=?,reviewed_at=? WHERE id=?", (payload["status"], note, session["user_id"], iso_now(), attendance_id))
            self.audit(con, session["user_id"], "review", {"id": attendance_id, "status": payload["status"], "note": note})
            self.send_json({"ok": True})
            return
        if action == "clarify" and self.command == "POST":
            if row["user_id"] != session["user_id"] or row["status"] != "flagged":
                raise AppError("Klarifikasi hanya untuk presensi Anda yang bermasalah.", HTTPStatus.FORBIDDEN)
            note = text_value(self.read_json().get("note"), 5, 1000, "Klarifikasi")
            con.execute("UPDATE attendance SET clarification=?,status='clarified' WHERE id=?", (note, attendance_id))
            self.audit(con, session["user_id"], "clarify", {"id": attendance_id, "note": note})
            self.send_json({"ok": True})
            return
        raise AppError("Endpoint tidak ditemukan.", HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ruang Presensi Cafe")
    parser.add_argument("--init", action="store_true", help="buat akun admin pertama")
    args = parser.parse_args()
    initialize_database()
    if args.init:
        try:
            create_admin()
        except AppError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc
        print("Admin berhasil dibuat.")
        return
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "3000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Ruang Presensi (Python): http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
