"""One configured administrator; everyone else has read-only application access."""
from __future__ import annotations

import hashlib
import hmac
import imaplib
import ssl
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

COOKIE = "dashboard_admin"
LIFETIME = 8 * 3600
LOCK = threading.RLock()
SESSIONS: dict[str, tuple[float, str]] = {}
ATTEMPTS: dict[str, list[float]] = {}
router = APIRouter(prefix="/api/admin")


def credentials():
    from hospkart_chat.integration import vendor_setting
    return vendor_setting("ADMIN_EMAIL", vendor_setting("ADMIN_USERNAME", "admin")).strip().casefold(), vendor_setting("ADMIN_PASSWORD")


def auth_provider():
    from hospkart_chat.integration import vendor_setting
    return vendor_setting("ADMIN_AUTH_PROVIDER", "password").strip().casefold()


def configured():
    email, password = credentials()
    return bool(email and (auth_provider() == "hostinger" or (auth_provider() == "password" and password)))


def verify_credentials(email: str, password: str) -> bool:
    expected_email, expected_password = credentials()
    if not hmac.compare_digest(email.encode(), expected_email.encode()):
        return False
    if not password or any(ord(char) < 32 or ord(char) == 127 for char in email + password):
        return False
    if auth_provider() == "password":
        return hmac.compare_digest(password.encode(), expected_password.encode())
    if auth_provider() != "hostinger":
        raise HTTPException(503, "Admin sign-in is not configured.")
    # Authenticate only: never SELECT, SEARCH, FETCH or STORE mailbox messages.
    # The mailbox password is neither persisted nor included in the session.
    try:
        with imaplib.IMAP4_SSL("imap.hostinger.com", 993,
                              ssl_context=ssl.create_default_context(), timeout=10) as mailbox:
            status, _ = mailbox.login(email, password)
            return status == "OK"
    except (imaplib.IMAP4.abort, OSError):
        raise HTTPException(503, "Hostinger Mail is unavailable. Please try again shortly.") from None
    except (imaplib.IMAP4.error, UnicodeEncodeError):
        return False


def fingerprint():
    username, password = credentials()
    return hashlib.sha256((auth_provider() + "\0" + username + "\0" + password).encode()).hexdigest()


def is_admin(token: str = "") -> bool:
    if not isinstance(token, str) or not token or not configured():
        return False
    key = hashlib.sha256(token.encode()).hexdigest()
    with LOCK:
        record = SESSIONS.get(key)
        if record and record[0] > time.time() and hmac.compare_digest(record[1], fingerprint()):
            return True
        SESSIONS.pop(key, None)
    return False


def same_origin(request: Request):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.url.netloc:
        raise HTTPException(403, "Use the dashboard on the same site.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Use the dashboard on the same site.")


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)


@router.get("/session")
def session(request: Request):
    response = JSONResponse({"admin": is_admin(request.cookies.get(COOKIE, "")), "configured": configured(), "provider": auth_provider()})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/login")
def login(request: Request, body: Login):
    same_origin(request)
    if not configured():
        raise HTTPException(503, "Admin sign-in is not configured on this server.")
    address = request.client.host if request.client else "unknown"
    now = time.time()
    with LOCK:
        # Bound memory and rate-limit failures across browser sessions.
        for key in list(ATTEMPTS):
            ATTEMPTS[key] = [t for t in ATTEMPTS[key] if t > now - 300]
            if not ATTEMPTS[key]:
                del ATTEMPTS[key]
        if len(ATTEMPTS.get(address, [])) >= 5 or sum(map(len, ATTEMPTS.values())) >= 100:
            raise HTTPException(429, "Too many attempts. Try again in five minutes.")
        # Reserve before network I/O so concurrent attempts are also limited.
        ATTEMPTS.setdefault(address, []).append(now)
    version = fingerprint()
    if not verify_credentials(body.email.strip().casefold(), body.password):
        raise HTTPException(401, "Incorrect email or mailbox password." if auth_provider() == "hostinger" else "Incorrect admin credentials.")
    with LOCK:
        if not hmac.compare_digest(version, fingerprint()):
            raise HTTPException(503, "Sign-in settings changed. Please retry.")
        ATTEMPTS.pop(address, None)
        for key in list(SESSIONS):
            if SESSIONS[key][0] <= now:
                del SESSIONS[key]
        old = request.cookies.get(COOKIE, "")
        SESSIONS.pop(hashlib.sha256(old.encode()).hexdigest(), None)
        token = secrets.token_urlsafe(32)
        SESSIONS[hashlib.sha256(token.encode()).hexdigest()] = (now + LIFETIME, fingerprint())
    response = JSONResponse({"admin": True})
    response.set_cookie(COOKIE, token, max_age=LIFETIME, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/logout")
def logout(request: Request):
    same_origin(request)
    token = request.cookies.get(COOKIE, "")
    with LOCK:
        SESSIONS.pop(hashlib.sha256(token.encode()).hexdigest(), None)
    response = JSONResponse({"admin": False})
    response.delete_cookie(COOKIE, path="/")
    response.delete_cookie("dashboard_viewer", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


# These requests produce downloads or a personal assistant reply; they do not
# edit shared source records, configuration, approvals or stored documents.
USER_POSTS = {"/api/vendor-uploads/session", "/api/export", "/api/export/products", "/api/export/structured-zip",
              "/chatbot/chat", "/chatbot/api/chat"}


def needs_admin(method: str, path: str) -> bool:
    return (method not in {"GET", "HEAD", "OPTIONS"}
            and not path.startswith("/api/admin/")
            and path.rstrip("/") not in USER_POSTS
            and not (path == "/vendor" or path.startswith("/vendor/")))


@router.post("/viewer")
def viewer(request: Request):
    same_origin(request)
    response = JSONResponse({"admin": False})
    response.set_cookie("dashboard_viewer", "1", max_age=LIFETIME, httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return response
