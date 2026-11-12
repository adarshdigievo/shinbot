"""Bot identities, browser sharing and portable, data-free profile templates."""

import hashlib
import json
import time
from pathlib import Path
from random import choice
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import packed, uid
from .plugins import PERMISSIVE, relative_file

OFFICIAL_TEMPLATES = "adarshdigievo/shinbot-official-templates"
BotColor = Literal["blue", "teal", "amber", "rose", "violet", "slate"]
BOT_COLORS = get_args(BotColor)


def choose_color(used):
    counts = {color: used.count(color) for color in BOT_COLORS}
    least_used = min(counts.values())
    return choice([color for color, count in counts.items() if count == least_used])


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    color: BotColor | None = None
    description: str = Field(default="", max_length=500)
    instructions: str = Field(default="", max_length=12000)
    starters: list[str] = Field(default_factory=list, max_length=6)
    browser: Literal["private", "group", "shared"] = "private"
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=150)

    @model_validator(mode="after")
    def validate_profile(self):
        if not self.name.strip() or any(not s.strip() or len(s) > 1000 for s in self.starters):
            raise ValueError("Give the bot a name and keep starter prompts under 1,000 characters.")
        if bool(self.provider) != bool(self.model):
            raise ValueError("Choose both a provider and a model, or inherit the installation model.")
        return self


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    kind: Literal["bot", "group"] = "bot"
    license: str = "MIT"
    profiles: list[Profile] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_template(self):
        if self.license not in PERMISSIVE:
            raise ValueError("This template license needs a host review.")
        if self.kind == "bot" and len(self.profiles) != 1:
            raise ValueError("A bot template contains exactly one profile.")
        if self.kind == "bot" and self.profiles[0].browser == "group":
            raise ValueError("Group browser sharing requires a group template.")
        return self


class Bots:
    def __init__(self, db):
        self.db = db
        db.execute("INSERT OR IGNORE INTO bot_groups VALUES (?,?,?)", ("default", "My bots", time.time()))
        profile = Profile(
            name="ShinBot",
            color="blue",
            description="Your everyday assistant",
            browser="shared",
            starters=[
                "Help me choose a useful first task. Ask about my day and suggest three things you could help with.",
                "Research a topic and save the useful findings in a note.",
            ],
        )
        db.execute(
            "INSERT OR IGNORE INTO bots VALUES (?,?,?,?,?,?,?,?)",
            (
                "default",
                "default",
                profile.name,
                packed(profile.model_dump()),
                0,
                time.time(),
                time.time(),
                1,
            ),
        )
        root = Path(__file__).parent / "bundled" / "templates"
        existing = db.one("SELECT revision FROM template_markets WHERE source=?", (OFFICIAL_TEMPLATES,))
        if root.exists() and (not existing or existing["revision"].startswith("bundled-")):
            self.catalog(
                OFFICIAL_TEMPLATES,
                "bundled-0.1.0a1",
                {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()},
            )

    def get(self, bot_id):
        bot = self.db.one("SELECT * FROM bots WHERE id=?", (bot_id,))
        if not bot or bot["archived"]:
            raise ValueError("This bot is unavailable. Choose an active bot from Home.")
        bot["profile"] = json.loads(bot["profile"])
        bot["profile"]["color"] = bot["profile"].get("color") or "blue"
        bot["browser_id"] = self.browser_id(bot)
        return bot

    @staticmethod
    def browser_id(bot):
        mode = bot["profile"]["browser"]
        return (
            "shared"
            if mode == "shared"
            else ("group-" + bot["group_id"] if mode == "group" else "bot-" + bot["id"])
        )

    def list(self):
        return [
            self.get(row["id"])
            for row in self.db.all("SELECT id FROM bots WHERE archived=0 ORDER BY created")
        ]

    def save(self, data, bot_id=None, group_id=None):
        profile = Profile.model_validate(data)
        old = self.get(bot_id) if bot_id else None
        group_id = group_id or (old["group_id"] if old else "default")
        if not self.db.one("SELECT 1 FROM bot_groups WHERE id=?", (group_id,)):
            raise ValueError("Choose an existing bot group.")
        key = bot_id or uid()
        now = time.time()
        with self.db.transaction() as db:
            rows = db.execute("SELECT profile FROM bots WHERE archived=0").fetchall()
            if not old and len(rows) >= 50:
                raise ValueError("This installation supports up to 50 active bot profiles.")
            if profile.color is None:
                profile.color = (
                    old["profile"]["color"]
                    if old
                    else choose_color([json.loads(row["profile"]).get("color") or "blue" for row in rows])
                )
            db.execute(
                "INSERT INTO bots VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,profile=excluded.profile,group_id=excluded.group_id,updated=excluded.updated,revision=bots.revision+1",
                (
                    key,
                    group_id,
                    profile.name,
                    packed(profile.model_dump()),
                    0,
                    old["created"] if old else now,
                    now,
                    1,
                ),
            )
        return self.get(key)

    def archive(self, bot_id):
        if bot_id == "default":
            raise ValueError("Keep the default ShinBot profile. You can edit its role instead.")
        if self.db.one(
            "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','uncertain','paused')",
            (bot_id,),
        ):
            raise ValueError("Stop this bot’s active tasks before archiving it.")
        self.db.execute("UPDATE schedules SET enabled=0 WHERE bot_id=?", (bot_id,))
        self.db.execute("UPDATE bots SET archived=1 WHERE id=?", (bot_id,))

    def catalog(self, source, revision, files):
        catalog = json.loads(files.get("shinbot-templates.json", b"{}"))
        if (
            catalog.get("schema_version") != 1
            or not isinstance(catalog.get("templates"), list)
            or len(catalog["templates"]) > 100
        ):
            raise ValueError(
                "Repository needs a version 1 shinbot-templates.json catalog with up to 100 templates."
            )
        items = []
        seen = set()
        for entry in catalog["templates"]:
            path = str(relative_file(entry["path"]))
            content = files.get(path, b"")
            if len(content) > 200000 or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("Template hash mismatch. Ask its maintainer to rebuild the catalog.")
            template = Template.model_validate_json(content)
            key = (template.id, template.version)
            if key != (entry["id"], entry["version"]) or key in seen:
                raise ValueError("Template catalog contains a duplicate or mismatched identity.")
            seen.add(key)
            items.append(template.model_dump())
        existing = self.db.one("SELECT id FROM template_markets WHERE source=?", (source,))
        key = existing["id"] if existing else uid()
        self.db.execute(
            "INSERT INTO template_markets VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,templates=excluded.templates,updated=excluded.updated",
            (key, source, revision, packed(items), time.time()),
        )
        return {"id": key, "source": source, "revision": revision, "templates": items}

    def templates(self):
        catalogs = [
            {**row, "templates": json.loads(row["templates"])}
            for row in self.db.all("SELECT * FROM template_markets ORDER BY source")
        ]
        local = self.db.setting("local_templates", {})
        if local:
            catalogs.insert(
                0,
                {
                    "id": "local",
                    "source": "Saved templates",
                    "revision": "local",
                    "templates": list(local.values()),
                },
            )
        return catalogs

    def save_template(self, template):
        value = Template.model_validate(template).model_dump()
        from .security import Security

        Security(self.db).template(value)
        key = value["id"] + "@" + value["version"]
        local = self.db.setting("local_templates", {})
        if key in local and local[key] != value:
            raise ValueError("This template version exists. Increase its version to save changes.")
        if key not in local and len(local) >= 100:
            raise ValueError("The local library holds up to 100 templates.")
        local[key] = value
        self.db.execute("INSERT OR REPLACE INTO settings VALUES ('local_templates',?)", (packed(local),))
        return value

    def install(self, template, request_id, allow_shared_browser=False):
        template = Template.model_validate(template)
        from .security import Security

        Security(self.db).template(template.model_dump())
        if not allow_shared_browser:
            for profile in template.profiles:
                if profile.browser == "shared":
                    profile.browser = "group" if template.kind == "group" else "private"
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("Provide a unique installation request ID.")
        # Keep retry receipts from before optional colours were introduced valid.
        unset_colors = {i: {"color"} for i, profile in enumerate(template.profiles) if profile.color is None}
        digest = hashlib.sha256(
            template.model_dump_json(exclude={"profiles": unset_colors}).encode()
        ).hexdigest()
        with self.db.transaction() as db:
            prior = db.execute("SELECT * FROM template_installs WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior["hash"] != digest:
                    raise ValueError("Installation request was reused with different content.")
                return json.loads(prior["result"])
            if (
                db.execute("SELECT count(*) FROM bots WHERE archived=0").fetchone()[0]
                + len(template.profiles)
                > 50
            ):
                raise ValueError("This installation supports up to 50 active bots.")
            group_id = uid() if template.kind == "group" else "default"
            now = time.time()

            def available_name(name, used, limit):
                candidate, number = name, 2
                while candidate in used:
                    suffix = f" ({number})"
                    candidate = name[: limit - len(suffix)] + suffix
                    number += 1
                used.add(candidate)
                return candidate

            if template.kind == "group":
                names = {row["name"] for row in db.execute("SELECT name FROM bot_groups")}
                group_name = available_name(template.name, names, 100)
                db.execute("INSERT INTO bot_groups VALUES (?,?,?)", (group_id, group_name, now))
            ids = []
            names = {row["name"] for row in db.execute("SELECT name FROM bots WHERE archived=0")}
            used_colors = [
                json.loads(row["profile"]).get("color") or "blue"
                for row in db.execute("SELECT profile FROM bots WHERE archived=0")
            ] + [profile.color for profile in template.profiles if profile.color]
            for profile in template.profiles:
                profile.name = available_name(profile.name, names, 80)
                if profile.color is None:
                    profile.color = choose_color(used_colors)
                    used_colors.append(profile.color)
                key = uid()
                ids.append(key)
                db.execute(
                    "INSERT INTO bots VALUES (?,?,?,?,?,?,?,?)",
                    (key, group_id, profile.name, packed(profile.model_dump()), 0, now, now, 1),
                )
            result = {"bot_ids": ids, "group_id": group_id}
            db.execute("INSERT INTO template_installs VALUES (?,?,?)", (request_id, digest, packed(result)))
        return result

    def export(self, bot_id=None, group_id=None):
        bots = [self.get(bot_id)] if bot_id else [b for b in self.list() if b["group_id"] == group_id]
        if not bots:
            raise ValueError("No active profiles to export.")
        group = self.db.one("SELECT name FROM bot_groups WHERE id=?", (group_id,)) if group_id else None
        profiles = [dict(b["profile"]) for b in bots]
        if bot_id and profiles[0]["browser"] == "group":
            profiles[0]["browser"] = "private"
        return Template(
            id="my-bot" if bot_id else "my-bot-group",
            version="1.0.0",
            name=bots[0]["name"] if bot_id else group["name"],
            description="A shared ShinBot profile template. Review instructions before publishing.",
            kind="bot" if bot_id else "group",
            profiles=profiles,
        ).model_dump()
