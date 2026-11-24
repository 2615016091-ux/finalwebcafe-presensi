import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.port = cls.free_port()
        cls.env = os.environ | {
            "DATA_DIR": cls.temp.name,
            "HOST": "127.0.0.1",
            "PORT": str(cls.port),
            "ADMIN_EMAIL": "admin@test.local",
            "ADMIN_PASSWORD": "test-admin-password",
        }
        init = subprocess.run([sys.executable, "app.py", "--init"], cwd=ROOT, env=cls.env, capture_output=True, text=True)
        if init.returncode != 0:
            raise RuntimeError(init.stderr)
        cls.start_server()

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()
        cls.temp.cleanup()

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @classmethod
    def start_server(cls):
        cls.server = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/")
                if conn.getresponse().status == 200:
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("Server Python tidak dapat dimulai")

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()

    @classmethod
    def request(cls, path, method="GET", data=None, session=None):
        headers = {"Content-Type": "application/json"}
        if session:
            headers["Cookie"] = session.get("cookie", "")
            headers["X-CSRF-Token"] = session.get("csrf", "")
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        conn.request(method, "/api" + path, json.dumps(data).encode() if data is not None else None, headers)
        response = conn.getresponse()
        raw = response.read()
        body = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        cookie = response.getheader("Set-Cookie")
        return response.status, body, cookie.split(";", 1)[0] if cookie else None

    @classmethod
    def login(cls, email, password):
        status, _, cookie = cls.request("/login", "POST", {"email": email, "password": password})
        assert status == 200
        session = {"cookie": cookie}
        status, body, _ = cls.request("/me", session=session)
        assert status == 200
        session["csrf"] = body["csrf"]
        return session

    def test_complete_workflow_and_persistence(self):
        status, _, _ = self.request("/attendance")
        self.assertEqual(status, 401)
        admin = self.login("admin@test.local", "test-admin-password")
        settings = {"name": "Test Coffee", "lat": -6.0, "lng": 106.0, "radius": 100, "max_accuracy": 75, "timezone": "Asia/Jakarta"}
        self.assertEqual(self.request("/settings", "PUT", settings, admin)[0], 200)
        user = {"name": "Barista Test", "email": "staff@test.local", "password": "employee-password", "role": "employee"}
        self.assertEqual(self.request("/users", "POST", user, admin)[0], 201)
        staff = self.login("staff@test.local", "employee-password")
        self.assertEqual(self.request("/users", session=staff)[0], 403)

        photo = bytearray(1200)
        photo[:2], photo[-2:] = b"\xff\xd8", b"\xff\xd9"
        event = {"kind": "in", "lat": -6.0, "lng": 106.0, "accuracy": 12, "photo": "data:image/jpeg;base64," + base64.b64encode(photo).decode()}
        self.assertEqual(self.request("/attendance", "POST", event | {"accuracy": 200}, staff)[0], 400)
        self.assertEqual(self.request("/attendance", "POST", event, staff)[0], 201)
        self.assertEqual(self.request("/attendance", "POST", event, staff)[0], 400)
        rows = self.request("/attendance", session=staff)[1]
        attendance_id = rows[0]["id"]
        self.assertEqual(self.request(f"/attendance/{attendance_id}/review", "POST", {"status": "flagged", "note": "Foto kurang jelas."}, admin)[0], 200)
        self.assertEqual(self.request(f"/attendance/{attendance_id}/clarify", "POST", {"note": "Pencahayaan sedang redup."}, staff)[0], 200)
        self.assertEqual(self.request(f"/attendance/{attendance_id}/review", "POST", {"status": "approved", "note": "Sudah dikonfirmasi."}, admin)[0], 200)
        self.assertEqual(self.request("/attendance", "POST", event | {"kind": "out"}, staff)[0], 201)

        self.stop_server()
        self.start_server()
        rows = self.request("/attendance", session=staff)[1]
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(row for row in rows if row["id"] == attendance_id)["status"], "approved")


if __name__ == "__main__":
    unittest.main()
