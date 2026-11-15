"""Optional Google Calendar connection with owner-bound OAuth and a combined agenda."""

import asyncio
import base64
import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit

from . import integrations
from .auth import digest

SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


def window(start, end):
    try:
        begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if not begin.tzinfo or not finish.tzinfo or not timedelta(0) < finish - begin <= timedelta(days=93):
            raise ValueError()
        return begin, finish
    except (TypeError, AttributeError, ValueError):
        raise ValueError("Choose a date range of up to 93 days, with UTC offsets.") from None


class Calendar:
    def __init__(self, core):
        self.core, self.db = core, core.db
        self.refresh_lock = asyncio.Lock()

    def require(self, bot_id=None):
        if not self.db.one("SELECT 1 FROM plugins WHERE id='calendar' AND status='active'"):
            raise ValueError("Install and activate Calendar in Plugins first.")
        if bot_id and not self.core.plugins.allowed("calendar", bot_id):
            raise ValueError("Enable Calendar for this bot in Plugins first.")

    @property
    def vault(self):
        return self.core.plugins.vault

    @property
    def redirect_uri(self):
        return self.core.config.origin + "/api/calendar/oauth/callback"

    def settings(self, changes=None):
        self.require()
        if changes is not None:
            if not isinstance(changes, dict) or set(changes) - {"client_id", "client_secret", "calendars"}:
                raise ValueError("Choose Google client settings and calendar IDs.")
            if "calendars" in changes:
                ids = changes["calendars"]
                if (
                    not isinstance(ids, list)
                    or not 1 <= len(ids) <= 10
                    or any(
                        not isinstance(x, str) or not x or len(x) > 320 or any(ord(c) < 32 for c in x)
                        for x in ids
                    )
                ):
                    raise ValueError("Choose between one and ten calendar IDs.")
            credentials = {k: changes[k] for k in ("client_id", "client_secret") if k in changes}
            if credentials:
                self.vault.update(
                    "google-calendar",
                    {**credentials, "refresh_token": None, "access_token": None, "expires": None},
                )
                self.db.execute("DELETE FROM oauth_pending")
            if "calendars" in changes:
                self.db.set("calendar-ids", list(dict.fromkeys(changes["calendars"])))
        values = self.vault.read("google-calendar")
        return {
            "configured": bool(values.get("client_id") and values.get("client_secret")),
            "connected": bool(values.get("refresh_token")),
            "calendars": self.db.setting("calendar-ids", ["primary"]),
            "redirect_uri": self.redirect_uri,
            "client_id": values.get("client_id", ""),
            "scope": "Read-only Google Calendar",
        }

    def begin(self, session):
        self.require()
        values = self.vault.read("google-calendar")
        if not values.get("client_id") or not values.get("client_secret"):
            raise ValueError("Save your Google OAuth client ID and client secret first.")
        parsed = urlsplit(self.core.config.origin)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "[::1]", "::1"}:
            raise ValueError("Google sign-in requires HTTPS, or a localhost development address.")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.db.execute(
            "DELETE FROM oauth_pending WHERE expires<? OR session_hash=?", (time.time(), digest(session))
        )
        self.db.execute(
            "INSERT INTO oauth_pending VALUES (?,?,?,?)",
            (digest(state), digest(session), verifier, time.time() + 600),
        )
        return {
            "url": "https://accounts.google.com/o/oauth2/v2/auth?"
            + urlencode(
                {
                    "client_id": values["client_id"],
                    "redirect_uri": self.redirect_uri,
                    "response_type": "code",
                    "scope": SCOPE,
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        }

    async def finish(self, session, state, code, error=None):
        return await self._finish(digest(session), state, code, error)

    async def finish_browser(self, cookie, state, code, error=None):
        if not cookie or not state or not secrets.compare_digest(cookie.encode(), state.encode()):
            raise ValueError("This sign-in link does not belong to this browser. Connect Google again.")
        row = self.db.one("SELECT session_hash FROM oauth_pending WHERE state=?", (digest(state),))
        if not row or not self.db.one(
            "SELECT 1 FROM sessions WHERE hash=? AND expires>?", (row["session_hash"], time.time())
        ):
            raise ValueError("Your sign-in session expired. Sign in to ShinBot and connect Google again.")
        return await self._finish(row["session_hash"], state, code, error)

    async def _finish(self, session_hash, state, code, error=None):
        self.require()
        with self.db.transaction() as sql:
            row = sql.execute(
                "SELECT * FROM oauth_pending WHERE state=? AND session_hash=? AND expires>?",
                (digest(state), session_hash, time.time()),
            ).fetchone()
            if not row:
                raise ValueError(
                    "This sign-in link expired or belongs to another session. Connect Google again."
                )
            sql.execute("DELETE FROM oauth_pending WHERE state=?", (digest(state),))
        if error or not code:
            raise ValueError("Google sign-in was cancelled. You can connect again from Calendar.")
        values = self.vault.read("google-calendar")
        async with integrations.client() as client:
            tokens, _ = await integrations.request_json(
                client,
                "POST",
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": values.get("client_id", ""),
                    "client_secret": values.get("client_secret", ""),
                    "code": code,
                    "code_verifier": row["verifier"],
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                },
            )
        if (
            not tokens.get("refresh_token")
            or not tokens.get("access_token")
            or SCOPE not in tokens.get("scope", "").split()
        ):
            raise ValueError(
                "Google did not grant offline calendar access. Reconnect and approve Calendar access."
            )
        if values != self.vault.read("google-calendar"):
            raise ValueError("Calendar settings changed during sign-in. Connect again.")
        self.vault.update(
            "google-calendar",
            {k: str(tokens[k]) for k in ("access_token", "refresh_token")}
            | {"expires": str(time.time() + int(tokens.get("expires_in", 3600)))},
        )
        self.db.set("calendar-ids", ["primary"])

    def disconnect(self):
        self.require()
        self.vault.update("google-calendar", {"access_token": None, "refresh_token": None, "expires": None})
        self.db.execute("DELETE FROM oauth_pending")
        return {"disconnected": True}

    async def token(self):
        async with self.refresh_lock:
            values = self.vault.read("google-calendar")
            if not values.get("refresh_token"):
                raise ValueError("Connect Google in Calendar first.")
            if values.get("access_token") and float(values.get("expires", 0)) > time.time() + 60:
                return values["access_token"]
            async with integrations.client() as client:
                data, _ = await integrations.request_json(
                    client,
                    "POST",
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": values["client_id"],
                        "client_secret": values["client_secret"],
                        "refresh_token": values["refresh_token"],
                        "grant_type": "refresh_token",
                    },
                )
            if not data.get("access_token"):
                raise ValueError("Google authorization expired. Connect Google again.")
            if values != self.vault.read("google-calendar"):
                raise ValueError("Calendar connection changed. Refresh again.")
            self.vault.update(
                "google-calendar",
                {
                    "access_token": data["access_token"],
                    "expires": str(time.time() + int(data.get("expires_in", 3600))),
                },
            )
            return data["access_token"]

    async def calendars(self):
        self.require()
        identity = self.vault.read("google-calendar").get("refresh_token")
        token = await self.token()
        result, cursor = [], None
        async with integrations.client() as client:
            client.headers["Authorization"] = "Bearer " + token
            for _ in range(5):
                data, _ = await integrations.request_json(
                    client,
                    "GET",
                    "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                    params={"maxResults": 250, **({"pageToken": cursor} if cursor else {})},
                )
                result.extend(
                    {"id": c["id"], "name": c.get("summary", c["id"]), "primary": c.get("primary", False)}
                    for c in data.get("items", [])
                    if not c.get("deleted")
                )
                cursor = data.get("nextPageToken")
                if not cursor:
                    break
        self.require()
        if identity != self.vault.read("google-calendar").get("refresh_token"):
            raise ValueError("Calendar connection changed during the request.")
        return {"calendars": result, "truncated": bool(cursor)}

    async def agenda(self, start, end, bot_id=None):
        self.require(bot_id)
        begin, finish = window(start, end)
        items, warnings = [], []
        settings = self.settings()
        identity = self.vault.read("google-calendar").get("refresh_token")
        if settings["connected"]:
            try:
                token = await self.token()
                async with integrations.client() as client:
                    client.headers["Authorization"] = "Bearer " + token
                    for calendar_id in settings["calendars"]:
                        try:
                            cursor = None
                            for _ in range(5):
                                params = {
                                    "timeMin": begin.isoformat(),
                                    "timeMax": finish.isoformat(),
                                    "singleEvents": "true",
                                    "orderBy": "startTime",
                                    "maxResults": 250,
                                    **({"pageToken": cursor} if cursor else {}),
                                }
                                data, _ = await integrations.request_json(
                                    client,
                                    "GET",
                                    "https://www.googleapis.com/calendar/v3/calendars/"
                                    + quote(calendar_id, safe="")
                                    + "/events",
                                    params=params,
                                )
                                for event in data.get("items", []):
                                    if event.get("status") == "cancelled":
                                        continue
                                    link = event.get("htmlLink", "")
                                    items.append(
                                        {
                                            "id": event["id"],
                                            "source": "google",
                                            "calendar": calendar_id,
                                            "title": str(event.get("summary", "(No title)"))[:500],
                                            "start": event.get("start", {}),
                                            "end": event.get("end", {}),
                                            "location": str(event.get("location", ""))[:1000],
                                            "description": str(event.get("description", ""))[:6000],
                                            "url": link
                                            if urlsplit(link).scheme == "https"
                                            and urlsplit(link).hostname
                                            in {"calendar.google.com", "www.google.com"}
                                            else "",
                                        }
                                    )
                                cursor = data.get("nextPageToken")
                                if not cursor:
                                    break
                            if cursor:
                                warnings.append(
                                    f"{calendar_id}: results limited to 1,250 events. Choose a shorter range."
                                )
                        except ValueError as exc:
                            warnings.append(f"{calendar_id}: {exc}")
                if identity != self.vault.read("google-calendar").get("refresh_token") or settings[
                    "calendars"
                ] != self.db.setting("calendar-ids", ["primary"]):
                    items = []
                    warnings.append(
                        "The Google Calendar connection changed during this request. Refresh again."
                    )
            except ValueError as exc:
                warnings.append(str(exc))
        self.require(bot_id)
        query = "SELECT id,name,next_run,kind,bot_id FROM schedules WHERE enabled=1 AND next_run>=? AND next_run<?"
        args = [begin.timestamp(), finish.timestamp()]
        if bot_id:
            query += " AND bot_id=?"
            args.append(bot_id)
        for job in self.db.all(query, args):
            items.append(
                {
                    "id": job["id"],
                    "source": "shinbot",
                    "title": job["name"],
                    "kind": job["kind"],
                    "bot_id": job["bot_id"],
                    "start": {"dateTime": datetime.fromtimestamp(job["next_run"], timezone.utc).isoformat()},
                    "end": {},
                }
            )

        def sort_time(item):
            value = item["start"].get("dateTime")
            if value:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            return (
                datetime.fromisoformat(item["start"].get("date", "1970-01-01"))
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )

        items.sort(key=sort_time)
        return {
            "items": items,
            "warnings": warnings,
            "connection": settings,
            "note": "Recurring ShinBot jobs show their next scheduled run. Google all-day event end dates are exclusive.",
        }

    async def tool(self, name, args, task):
        self.require(task["bot_id"])
        if name == "list_events":
            return await self.agenda(args["start"], args["end"], task["bot_id"])
        if name == "list_calendars":
            return await self.calendars()
        raise ValueError("Unknown Calendar tool.")
