import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from .config import secret_file


def digest(token: str):
    return hashlib.sha256(token.encode()).hexdigest()


def verify_password(password: str, encoded: str):
    salt, expected = encoded.split(":")
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(expected, got)


def hash_password(password: str):
    if len(password) < 12 or len(password) > 256:
        raise ValueError("Use a password between 12 and 256 characters.")
    salt = secrets.token_bytes(16)
    return salt.hex() + ":" + hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()


class Auth:
    def __init__(self, db, config):
        self.db, self.config = db, config
        self.bootstrap = secret_file(config.data / "bootstrap-token")
        self.attempts = {}

    def rate_limit(self, host):
        now = time.time()
        self.attempts = {k: v for k, v in self.attempts.items() if v[-1] > now - 60}
        attempts = [t for t in self.attempts.get(host, []) if t > now - 60]
        if len(attempts) >= 8:
            raise HTTPException(429, "Too many attempts. Try again in a minute.")
        self.attempts[host] = attempts + [now]

    def session(self, kind="browser"):
        token = secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO sessions VALUES (?,?,?)",
            (digest(token), time.time() + (86400 * 7 if kind == "browser" else 86400 * 30), kind),
        )
        return token

    def require(self, request: Request):
        header = request.headers.get("authorization", "")
        bearer = header.startswith("Bearer ")
        token = header[7:] if bearer else request.cookies.get("shinbot_session", "")
        row = self.db.one("SELECT * FROM sessions WHERE hash=? AND expires>?", (digest(token), time.time()))
        if not row:
            raise HTTPException(401, "Sign in to ShinBot.")
        if not bearer and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if (
                request.headers.get("origin") != self.config.origin
                or request.headers.get("x-shinbot-csrf") != "1"
            ):
                raise HTTPException(403, "Untrusted request origin.")
        return token


def require_service(request: Request, token: str):
    if not token or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
        raise HTTPException(401, "Invalid service credential.")
