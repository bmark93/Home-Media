"""Authentication and browser request protection for the management plane."""

import base64
import binascii
import re
import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def validate_credentials(user, password):
    placeholders = {"admin", "change-me", "changeme", "password", "passwordpassword",
                    "example", "your-password", "changeme-token"}
    if (
        not isinstance(user, str) or not isinstance(password, str)
        or len(user.strip()) < 3 or ":" in user
        or len(password.strip()) < 16 or len(set(password)) < 8
        or user.strip().lower() in placeholders or password.strip().lower() in placeholders
        or any(ord(c) < 32 or ord(c) == 127 for c in user + password)
    ):
        raise ValueError(
            "Set LINKER_USER and LINKER_PASS to non-example credentials: "
            "username must have at least 3 characters and no colon; password "
            "must have at least 16 characters and 8 distinct characters; no control characters."
        )
    return user, password


def password_matches(value, expected):
    try:
        return isinstance(value, str) and secrets.compare_digest(
            value.encode("utf-8"), expected.encode("utf-8")
        )
    except UnicodeError:
        return False


def authenticated(header, user, password):
    try:
        scheme, encoded = header.split()
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        supplied_user, supplied_password = decoded.split(":", 1)
    except (ValueError, UnicodeError, binascii.Error):
        return False
    user_ok = password_matches(supplied_user, user)
    password_ok = password_matches(supplied_password, password)
    return user_ok & password_ok


def normalized_origin(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None
                or url.path or url.query or url.fragment):
            return None
        port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
        return url.scheme, url.hostname.lower(), port
    except ValueError:
        return None


def valid_token(value):
    return re.fullmatch(r"[a-f0-9]{64}", value) is not None


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await self.protect(request, call_next)
        response.headers.update(SECURITY_HEADERS)
        return response

    async def protect(self, request, call_next):
        user, password = request.app.state.credentials
        if not authenticated(request.headers.get("authorization", ""), user, password):
            return JSONResponse(
                {"detail": "Authentication required"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Linker", charset="UTF-8"'},
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = normalized_origin(request.headers.get("origin", ""))
            expected = normalized_origin(f"{request.url.scheme}://{request.url.netloc}")
            token = request.headers.get("x-csrf-token", "")
            cookie = request.cookies.get("linker_csrf", "")
            if (origin is None or origin != expected or not valid_token(token)
                    or not valid_token(cookie) or not secrets.compare_digest(token, cookie)):
                return JSONResponse({"detail": "Invalid request origin or CSRF token"}, status_code=403)
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return JSONResponse({"detail": "JSON content type required"}, status_code=415)
        if request.method == "GET" and request.url.path == "/":
            token = request.cookies.get("linker_csrf", "")
            request.state.csrf_token = token if valid_token(token) else secrets.token_hex(32)
        response = await call_next(request)
        if request.method == "GET" and request.url.path == "/":
            response.set_cookie("linker_csrf", request.state.csrf_token, httponly=True,
                                samesite="strict", secure=request.url.scheme == "https")
            response.headers["Cache-Control"] = "no-store"
        return response
