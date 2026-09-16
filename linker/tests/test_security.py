import base64
import re

import pytest
from conftest import PASSWORD, USER

GETS = ["/", "/static/style.css", "/static/app.js", "/metrics", "/api/status", "/api/settings",
        "/api/cleanup/settings", "/api/keys", "/api/drives"]
POSTS = ["/api/relink", "/api/settings/auto-update", "/api/cleanup/settings",
         "/api/cleanup/run-now", "/api/drives/use", "/api/plex-claim", "/api/update-now"]


def assert_no_mutations(main):
    for name in ("run_cleanup_now", "update_cleanup_settings", "use_drive",
                 "set_plex_claim", "trigger_update", "recreate_services"):
        getattr(main, name).assert_not_called()
    main.SETTINGS.save.assert_not_called()
    assert main.run_linking.call_count == 1  # isolated startup only


@pytest.mark.parametrize("route", GETS + POSTS + ["/api/keys/reveal", "/api/unknown", "/docs", "/openapi.json"])
@pytest.mark.parametrize("authorization", [None, "Basic !!!", "Bearer synthetic",
    "Basic " + base64.b64encode(b"test-admin:wrong").decode(),
    "Basic " + base64.b64encode(b"wrong:Correct-Horse-Example-42!").decode(),
    "Basic /w==", "Basic bm9jb2xvbg=="])
def test_auth_required(client, isolated_app, route, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    response = client.request("POST" if route in POSTS or route == "/api/keys/reveal" else "GET", route, headers=headers, json={})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")
    assert_no_mutations(isolated_app)


@pytest.mark.parametrize("route", GETS)
def test_authenticated_get(client, route):
    assert client.get(route, auth=(USER, PASSWORD)).status_code == 200


@pytest.mark.parametrize("field,value", [
    ("LINKER_USER", ""), ("LINKER_PASS", ""), ("LINKER_USER", "admin"),
    ("LINKER_USER", "change-me"), ("LINKER_USER", "changeme"),
    ("LINKER_PASS", "admin"), ("LINKER_PASS", "change-me"),
    ("LINKER_PASS", "changeme"), ("LINKER_PASS", "short"),
    ("LINKER_PASS", "aaaaaaaaaaaaaaaa"), ("LINKER_PASS", "passwordpassword"),
    ("LINKER_USER", "ab"), ("LINKER_USER", "bad:user"),
    ("LINKER_USER", " admin "),
])
def test_startup_rejects_credentials(isolated_app, monkeypatch, field, value):
    from fastapi.testclient import TestClient
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError, match="LINKER_USER and LINKER_PASS"), TestClient(isolated_app.app):
        pass
    isolated_app.run_linking.assert_not_called()


def test_validator_is_pure_and_secret_free():
    from app import security
    validator = getattr(security, "validate_credentials", None)
    assert callable(validator), "credential validator is missing"
    assert validator(USER, PASSWORD) == (USER, PASSWORD)
    for user, password in [(None, PASSWORD), (USER, None), (USER, "short")]:
        with pytest.raises(ValueError) as exc:
            validator(user, password)
        assert PASSWORD not in str(exc.value)
        assert "short" not in str(exc.value)


TOKEN = "a" * 64


@pytest.mark.parametrize("route", POSTS)
@pytest.mark.parametrize("failure", ["origin-missing", "foreign", "null", "origin-path",
    "header-missing", "cookie-missing", "mismatch", "malformed", "content-type"])
def test_csrf_rejects_before_mutation(client, isolated_app, route, failure):
    headers = {"Origin": "http://testserver", "X-CSRF-Token": TOKEN,
               "Content-Type": "application/json"}
    cookie = TOKEN
    if failure == "origin-missing":
        del headers["Origin"]
    elif failure in ("foreign", "null", "origin-path"):
        headers["Origin"] = {"foreign": "http://foreign.invalid", "null": "null",
                             "origin-path": "http://testserver/path"}[failure]
    elif failure == "header-missing":
        del headers["X-CSRF-Token"]
    elif failure == "cookie-missing":
        cookie = None
    elif failure == "mismatch":
        headers["X-CSRF-Token"] = "b" * 64
    elif failure == "malformed":
        headers["X-CSRF-Token"] = cookie = "short"
    else:
        headers["Content-Type"] = "text/plain"
    if cookie:
        client.cookies.set("linker_csrf", cookie)
    response = client.post(route, auth=(USER, PASSWORD), headers=headers, content="{}")
    assert response.status_code == (415 if failure == "content-type" else 403)
    assert_no_mutations(isolated_app)


def csrf_headers(client):
    response = client.get("/", auth=(USER, PASSWORD))
    match = re.search(r'<meta name="csrf-token" content="([a-f0-9]{64})"', response.text)
    assert match, "root must inject a cryptographic CSRF token"
    return {"Origin": "http://testserver", "X-CSRF-Token": match[1]}


def test_csrf_cookie(client):
    headers = csrf_headers(client)
    response = client.get("/", auth=(USER, PASSWORD))
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert client.cookies["linker_csrf"] == headers["X-CSRF-Token"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route", POSTS)
def test_csrf_accepts_same_origin(client, isolated_app, route):
    headers = csrf_headers(client)
    headers["Origin"] = "http://TESTSERVER:80"
    assert client.post(route, auth=(USER, PASSWORD), headers=headers, json={}).status_code == 200
    target = {"/api/relink": isolated_app.run_linking,
              "/api/settings/auto-update": isolated_app.SETTINGS.save,
              "/api/cleanup/settings": isolated_app.update_cleanup_settings,
              "/api/cleanup/run-now": isolated_app.run_cleanup_now,
              "/api/drives/use": isolated_app.use_drive,
              "/api/plex-claim": isolated_app.set_plex_claim,
              "/api/update-now": isolated_app.trigger_update}[route]
    assert target.call_count == (2 if route == "/api/relink" else 1)


def test_dashboard_mutations_use_csrf(client):
    html = client.get("/", auth=(USER, PASSWORD)).text
    from pathlib import Path
    script = Path("app/static/app.js").read_text() if 'src="/static/app.js"' in html else html
    assert "X-CSRF-Token" in script
    assert "meta[name=" in script
    assert "apiFetch(" in script
    assert "await fetch(" not in script


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


@pytest.mark.parametrize("route", GETS + ["/static/app.js", "/api/missing"])
@pytest.mark.parametrize("logged_in", [True, False])
def test_security_headers(client, route, logged_in):
    response = client.get(route, auth=(USER, PASSWORD) if logged_in else None)
    assert response.headers.get("Content-Security-Policy") == CSP
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_external_script(client):
    response = client.get("/", auth=(USER, PASSWORD))
    assert '<script src="/static/app.js" defer></script>' in response.text
    assert all(not body.strip() for body in re.findall(r"<script[^>]*>(.*?)</script>", response.text, re.DOTALL))
    assert client.get("/static/app.js", auth=(USER, PASSWORD)).status_code == 200
    assert client.get("/static/app.js").status_code == 401
    for link in re.findall(r"<a\b[^>]*target=\"_blank\"[^>]*>", response.text):
        assert 'rel="noopener noreferrer"' in link


SENTINEL = "synthetic-api-key-never-auto-disclose"


@pytest.fixture
def key_info(isolated_app):
    isolated_app.get_connection_info.return_value = {
        "sonarr": {"host": "sonarr", "port": 8989, "api_key": SENTINEL},
        "radarr": {"host": "radarr", "port": 7878, "api_key": None},
    }
    return isolated_app.get_connection_info


@pytest.mark.parametrize("route", ["/", "/api/status", "/api/keys"])
def test_keys_are_not_automatically_disclosed(client, key_info, route):
    response = client.get(route, auth=(USER, PASSWORD))
    assert SENTINEL not in response.text
    if route == "/api/keys":
        assert response.json()["sonarr"] == {"host": "sonarr", "port": 8989, "key_available": True}
        assert response.json()["radarr"]["key_available"] is False


@pytest.mark.parametrize("body", [{}, {"password": "wrong"}, {"password": None},
                                  {"password": [PASSWORD]}, [], {"password": "\ud800"}])
def test_reveal_rejects_wrong_password(client, key_info, body):
    headers = csrf_headers(client)
    response = client.post("/api/keys/reveal", auth=(USER, PASSWORD), headers=headers, json=body)
    assert response.status_code == 403
    assert response.json() == {"detail": "Re-authentication failed"}
    assert SENTINEL not in response.text
    key_info.assert_not_called()


def test_reveal_requires_auth_and_csrf(client, key_info):
    assert client.post("/api/keys/reveal", json={"password": PASSWORD}).status_code == 401
    assert client.post("/api/keys/reveal", auth=(USER, PASSWORD), json={"password": PASSWORD}).status_code == 403
    key_info.assert_not_called()


def test_reveal_returns_keys_only_after_reauthentication(client, key_info):
    response = client.post("/api/keys/reveal", auth=(USER, PASSWORD),
                           headers=csrf_headers(client), json={"password": PASSWORD})
    assert response.status_code == 200
    assert response.json()["sonarr"]["api_key"] == SENTINEL
    assert response.headers["Cache-Control"] == "no-store"
    assert PASSWORD not in response.text
    assert SENTINEL not in client.get("/api/status", auth=(USER, PASSWORD)).text


def test_reveal_ui(client):
    html = client.get("/", auth=(USER, PASSWORD)).text
    script = client.get("/static/app.js", auth=(USER, PASSWORD)).text
    assert 'type="password" id="key-password"' in html
    assert 'id="reveal-keys-btn"' in html
    assert "'/api/keys/reveal'" in script
    assert "input.value = '';" in script
    assert "loadKeys" not in script
    assert "localStorage" not in script and "sessionStorage" not in script


def test_origin_port_zero_is_not_default_port(client, isolated_app):
    headers = csrf_headers(client)
    headers["Origin"] = "http://testserver:0"
    assert client.post("/api/cleanup/run-now", auth=(USER, PASSWORD), headers=headers, json={}).status_code == 403
    assert_no_mutations(isolated_app)


def test_security_headers_on_internal_error(isolated_app):
    from fastapi.testclient import TestClient
    isolated_app.list_drives.side_effect = RuntimeError("synthetic failure")
    with TestClient(isolated_app.app, raise_server_exceptions=False) as client:
        response = client.get("/api/drives", auth=(USER, PASSWORD))
    assert response.status_code == 500
    assert response.headers.get("Content-Security-Policy") == CSP
    assert "synthetic failure" not in response.text


def test_basic_compares_both_fields_and_accepts_utf8(monkeypatch):
    from unittest.mock import Mock

    from app import security
    compare = Mock(wraps=security.secrets.compare_digest)
    monkeypatch.setattr(security.secrets, "compare_digest", compare)
    encoded = base64.b64encode(f"wrong:{PASSWORD}".encode()).decode()
    assert not security.authenticated(f"Basic {encoded}", USER, PASSWORD)
    assert compare.call_count == 2
    user, password = "test-üser", PASSWORD + ":é"
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
    assert security.authenticated(f"bAsIc {encoded}", user, password)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_all_unsafe_methods_require_csrf(client, isolated_app, method):
    response = client.request(method, "/api/cleanup/run-now", auth=(USER, PASSWORD), json={})
    assert response.status_code == 403
    assert_no_mutations(isolated_app)


def test_https_csrf_cookie_and_independent_sessions(isolated_app):
    from fastapi.testclient import TestClient
    tokens = []
    for _ in range(2):
        with TestClient(isolated_app.app, base_url="https://testserver") as client:
            response = client.get("/", auth=(USER, PASSWORD))
            assert "Secure" in response.headers["set-cookie"]
            token = client.cookies["linker_csrf"]
            tokens.append(token)
            response = client.post("/api/cleanup/run-now", auth=(USER, PASSWORD), json={},
                                   headers={"Origin": "https://testserver:443", "X-CSRF-Token": token})
            assert response.status_code == 200
    assert tokens[0] != tokens[1]
