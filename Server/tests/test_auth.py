"""Access control (auth.py): device token, web login, secret key, and the
request size limit."""
from __future__ import annotations

import dataclasses
import json
from unittest import mock

import auth
from app import create_app
from tests.conftest import LRTestCase

TOKEN = "device-secret-123"
PASSWORD = "correct horse"


class _AuthCase(LRTestCase):
    #: Config overrides for the app under test.
    CFG: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self._cfg = dataclasses.replace(self._cfg, **self.CFG)
        self.app = create_app(self._cfg)
        self.app.config["AFTERWORD_PIPELINE_BACKGROUND"] = False
        self.client = self.app.test_client()

    def announce(self, headers=None):
        return self.client.post(
            "/api/v1/sessions",
            data=json.dumps({"id": "auth_session_1", "expected_count": 1}),
            content_type="application/json", headers=headers or {},
        )


class DeviceTokenTests(_AuthCase):
    CFG = {"device_token": TOKEN}

    def test_missing_or_wrong_token_is_rejected(self):
        self.assertEqual(self.announce().status_code, 401)
        bad = self.announce({"Authorization": "Bearer nope"})
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(bad.get_json()["error"], "unauthorized")

    def test_right_token_works_for_all_device_endpoints(self):
        h = {"Authorization": f"Bearer {TOKEN}"}
        self.assertEqual(self.announce(h).status_code, 201)
        put = self.client.put("/api/v1/sessions/auth_session_1/chunks/1",
                              data=b"\xff\xf1" + b"\x00" * 64, headers=h)
        self.assertNotEqual(put.status_code, 401)
        self.assertEqual(self.client.post(
            "/api/v1/sessions/auth_session_1/chunks/1").status_code, 405)
        fin = self.client.post("/api/v1/sessions/auth_session_1/finish")
        self.assertEqual(fin.status_code, 401)

    def test_web_ui_is_not_affected_without_a_password(self):
        self.assertEqual(self.client.get("/").status_code, 200)


class WebLoginTests(_AuthCase):
    CFG = {"web_password": PASSWORD, "device_token": TOKEN}

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(auth, "LOGIN_FAIL_DELAY_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pages_redirect_to_login_and_api_says_401(self):
        resp = self.client.get("/calendar")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login?next=/calendar", resp.headers["Location"])
        api = self.client.get("/api/v1/sessions")
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.get_json()["error"], "unauthorized")

    def test_login_page_and_static_are_open(self):
        self.assertEqual(self.client.get("/login").status_code, 200)
        css = self.client.get("/static/css/tokens.css")
        self.assertEqual(css.status_code, 200)
        css.close()

    def test_wrong_password(self):
        resp = self.client.post("/login", data={"password": "guess"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Wrong password", resp.get_data(as_text=True))
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_login_then_logout(self):
        resp = self.client.post("/login", data={"password": PASSWORD,
                                                "next": "/calendar"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/calendar"))
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Log out", page.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/v1/sessions").status_code, 200)
        self.client.post("/logout")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_next_cannot_leave_the_site(self):
        for target in ("//evil.example/", "https://evil.example/", "/\\evil"):
            resp = self.client.post("/login", data={"password": PASSWORD,
                                                    "next": target})
            self.assertEqual(resp.headers["Location"], "/", target)

    def test_device_endpoints_skip_the_login_but_need_the_token(self):
        self.assertEqual(self.announce().status_code, 401)
        ok = self.announce({"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(ok.status_code, 201)


class SecretKeyTests(_AuthCase):
    def test_generated_key_is_kept_across_restarts(self):
        key_file = self._tmpdir / "db" / "secret_key"
        self.assertTrue(key_file.exists())
        again = create_app(self._cfg)
        self.assertEqual(again.secret_key, self.app.secret_key)
        self.assertEqual(key_file.read_text().strip(), self.app.secret_key)
        self.assertGreaterEqual(len(self.app.secret_key), 64)

    def test_configured_key_wins(self):
        app = create_app(dataclasses.replace(self._cfg, secret_key="from-env"))
        self.assertEqual(app.secret_key, "from-env")


class SizeLimitTests(_AuthCase):
    def test_oversized_request_is_refused_as_json(self):
        # Flask enforces the limit when the body is read (waitress
        # already refuses it at the socket, see serve.py).
        self.assertEqual(self.announce().status_code, 201)
        limit = self.app.config["MAX_CONTENT_LENGTH"]
        resp = self.client.put("/api/v1/sessions/auth_session_1/chunks/1",
                               data=b"\x00" * (limit + 1))
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.get_json()["error"], "file_too_large")
