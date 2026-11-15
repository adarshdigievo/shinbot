import base64
import hashlib
import io
import json
import re
import shutil
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import packed, uid
from .errors import NotFound
from .security import Security, content_digest
from .vault import Vault

OFFICIAL = "adarshdigievo/shinbot-official-plugins"
PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense", "CC0-1.0"}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?$")
HOOK_EVENTS = {"before_chat", "after_response", "before_tool", "after_tool"}


class PluginPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    title: str = Field(min_length=1, max_length=60)
    icon: str = Field(default="puzzle", pattern=r"^[a-z][a-z0-9-]{0,47}$")
    entrypoint: str


class PluginUI(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pages: list[PluginPage] = Field(default_factory=list, max_length=20)
    styles: list[str] = Field(default_factory=list, max_length=20)
    patches: list[str] = Field(default_factory=list, max_length=20)
    assets: list[str] = Field(default_factory=list, max_length=100)


class PluginHook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str
    entrypoint: str
    timeout: int = Field(default=10, ge=1, le=60)

    @field_validator("event")
    @classmethod
    def known_event(cls, value):
        if value not in HOOK_EVENTS:
            raise ValueError("Unknown plugin lifecycle event.")
        return value


def relative_file(name):
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        raise ValueError("Plugin paths must be relative files without traversal.")
    if any(p.startswith(".") for p in path.parts):
        raise ValueError("Hidden plugin files are not accepted.")
    return path


class PluginSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^SHINBOT_PLUGIN_[A-Z][A-Z0-9_]{0,79}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    secret: bool = False
    required: bool = False


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1, le=1)
    id: str
    name: str = Field(min_length=1, max_length=100)
    version: str
    description: str = Field(min_length=1, max_length=1000)
    kind: str
    license: str
    files: list[str] = Field(min_length=1, max_length=100)
    permissions: list[str] = Field(default_factory=list, max_length=10)
    entrypoint: str | None = None
    tools: list[dict] = Field(default_factory=list, max_length=100)
    requires: list[str] = Field(default_factory=list)
    configuration: list[PluginSetting] = Field(default_factory=list, max_length=40)
    server: dict | None = None
    source: str | None = Field(default=None, max_length=2000)
    ui: PluginUI | None = None
    hooks: list[PluginHook] = Field(default_factory=list, max_length=4)

    @field_validator("configuration")
    @classmethod
    def unique_settings(cls, value):
        if len({item.key for item in value}) != len(value):
            raise ValueError("Plugin settings must have unique keys.")
        return value

    @field_validator("id")
    @classmethod
    def identifier(cls, value):
        if not IDENTIFIER.fullmatch(value) or "--" in value:
            raise ValueError("Use a lowercase plugin ID with letters, numbers and hyphens.")
        return value

    @field_validator("version")
    @classmethod
    def version_number(cls, value):
        if not VERSION.fullmatch(value):
            raise ValueError("Use a semantic version such as 1.0.0.")
        return value

    @field_validator("kind")
    @classmethod
    def plugin_kind(cls, value):
        if value not in {"skill", "mcp", "channel", "builtin", "extension"}:
            raise ValueError("Plugin kind must be skill, mcp, extension, channel or a bundled toolkit.")
        return value


def validate_package(files):
    if len(files) > 101 or sum(len(v) for v in files.values()) > 2_000_000:
        raise ValueError("Plugin exceeds the 2 MB or 101 file limit.")
    for path in files:
        relative_file(path)
    manifest = PluginManifest.model_validate_json(files.get("shinbot-plugin.json", b"{}"))
    if manifest.license not in PERMISSIVE:
        raise ValueError("This plugin license needs a host review before installation.")
    if manifest.requires:
        raise ValueError("Additional dependencies need a host license audit and a prepared runner image.")
    if set(files) != set(manifest.files) | {"shinbot-plugin.json"}:
        raise ValueError("Manifest files must match the package exactly.")
    if "LICENSE" not in files:
        raise ValueError("Include the plugin's LICENSE file.")
    if manifest.kind == "skill" and "SKILL.md" not in files:
        raise ValueError("A skill needs SKILL.md.")
    if manifest.kind in {"builtin", "channel"}:
        # These run inside ShinBot itself, so only the shipped packages may claim them.
        root = Path(__file__).parent / "bundled" / "official" / "plugins"
        directory = next(
            (
                p.parent
                for p in root.glob("*/shinbot-plugin.json")
                if (json.loads(p.read_text()).get("id"), json.loads(p.read_text()).get("version"))
                == (manifest.id, manifest.version)
            ),
            root / "missing",
        )
        if manifest.id not in {"kanban", "projects", "calendar", "telegram"} or not directory.is_dir():
            raise ValueError("Only bundled official toolkits can use native services.")
        canonical = {
            str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()
        }
        if files != canonical:
            raise ValueError("Bundled toolkit packages must match the shipped version exactly.")
    if manifest.ui:
        ui = manifest.ui
        if ui.pages and "ui.pages" not in manifest.permissions:
            raise ValueError("Sidebar pages need the ui.pages permission.")
        if ui.styles and "ui.styles" not in manifest.permissions:
            raise ValueError("App styles need the ui.styles permission.")
        if ui.patches and "ui.patch" not in manifest.permissions:
            raise ValueError("App patches need the ui.patch permission.")
        if len({page.id for page in ui.pages}) != len(ui.pages):
            raise ValueError("Sidebar page IDs must be unique within a plugin.")
        for name in [*(p.entrypoint for p in ui.pages), *ui.patches, *ui.styles, *ui.assets]:
            relative_file(name)
            if name not in files or PurePosixPath(name).suffix.lower() not in {
                ".js",
                ".css",
                ".json",
                ".png",
                ".jpg",
                ".jpeg",
                ".svg",
                ".webp",
                ".woff",
                ".woff2",
            }:
                raise ValueError("Declare each UI asset in files and use a supported web file type.")
        if any(not name.endswith(".js") for name in [*(p.entrypoint for p in ui.pages), *ui.patches]):
            raise ValueError("Pages and patches need JavaScript module entrypoints.")
        if any(not name.endswith(".css") for name in ui.styles):
            raise ValueError("App styles need CSS files.")
    if manifest.hooks:
        if "hooks" not in manifest.permissions:
            raise ValueError("Lifecycle hooks need the hooks permission.")
        if len({hook.event for hook in manifest.hooks}) != len(manifest.hooks):
            raise ValueError("Use one handler per lifecycle event in a plugin.")
        for hook in manifest.hooks:
            if hook.entrypoint not in files or not hook.entrypoint.endswith(".py"):
                raise ValueError("Hooks need a Python entrypoint within the package.")
            try:
                compile(files[hook.entrypoint], hook.entrypoint, "exec")
            except SyntaxError as exc:
                raise ValueError(f"Hook syntax error in {hook.entrypoint} on line {exc.lineno}.") from None
    if manifest.kind == "extension" and not (manifest.ui or manifest.hooks or manifest.tools):
        raise ValueError("An extension must provide UI, hooks or tools.")
    if manifest.kind == "mcp" or (manifest.kind == "extension" and manifest.tools):
        if manifest.server:
            from .extensions import MCPServer

            MCPServer.model_validate(manifest.server)
            if manifest.entrypoint:
                raise ValueError("Choose a server connection or a Python entrypoint.")
        else:
            if manifest.entrypoint not in files or not manifest.entrypoint.endswith(".py"):
                raise ValueError("MCP plugins need a Python entrypoint within the package.")
            try:
                compile(files[manifest.entrypoint], manifest.entrypoint, "exec")
            except SyntaxError as exc:
                raise ValueError(
                    f"{manifest.entrypoint} has a Python syntax error on line {exc.lineno}: {exc.msg}"
                ) from None
        names = set()
        for tool in manifest.tools:
            name = tool.get("name", "")
            pattern = r"[A-Za-z0-9_.-]{1,128}" if manifest.server else r"[A-Za-z][A-Za-z0-9_-]{0,47}"
            if not re.fullmatch(pattern, name) or name in names:
                raise ValueError("Tool names must be unique MCP identifiers.")
            if not manifest.server and len(f"plg__{manifest.id}__{name}") > 64:
                raise ValueError("Plugin ID plus tool name exceeds the provider tool-name limit.")
            names.add(name)
            if tool.get("input_schema", {}).get("type") != "object":
                raise ValueError("Each tool needs an object input schema.")
        if not manifest.tools:
            raise ValueError("Declare at least one MCP tool.")
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return manifest, digest.hexdigest()


def unpack_archive(data):
    if len(data) > 10_000_000:
        raise ValueError("Marketplace archive exceeds 10 MB.")
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 1000 or sum(x.file_size for x in archive.infolist()) > 20_000_000:
            raise ValueError("Marketplace expanded size exceeds the limit.")
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Symlinks are not accepted in marketplaces.")
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                raise ValueError("Archive path traversal is not allowed.")
            name = "/".join(PurePosixPath(entry.filename).parts[1:])
            if not name:
                continue
            if name in files:
                raise ValueError("Archive contains duplicate paths.")
            files[name] = archive.read(entry)
    return files


class Plugins:
    def __init__(self, db, directory):
        self.db, self.directory = db, directory
        self.validated = set()
        self.vault = Vault(directory.parent / "secrets")
        self.db.execute(
            "INSERT OR IGNORE INTO marketplaces(id,source,catalog) VALUES ('official',?,'{}')", (OFFICIAL,)
        )
        bundled = Path(__file__).parent / "bundled" / "official"
        official = self.db.one("SELECT revision FROM marketplaces WHERE source=?", (OFFICIAL,))
        if bundled.exists() and (official["revision"] is None or official["revision"].startswith("bundled-")):
            self.add_catalog(
                OFFICIAL,
                "bundled-0.1.0a2-telegram2",
                {
                    str(p.relative_to(bundled)): p.read_bytes()
                    for p in bundled.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
                },
            )
        # Telegram 1.x needed a separate host service that no longer exists.
        # They were never enabled, so no conversation refers to them.
        self.db.execute("DELETE FROM plugins WHERE id='telegram' AND version LIKE '1.%' AND status='staged'")

    async def repository(self, source, ref="HEAD"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
            raise ValueError("Use a GitHub repository in owner/repository form.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", ref):
            raise ValueError("Use a commit, tag or simple branch name.")
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(
                f"https://api.github.com/repos/{source}/commits/{ref}",
                headers={"Accept": "application/vnd.github+json"},
            )
            if response.status_code == 404:
                raise ValueError(
                    "Repository or revision not found. Check owner/repository and use a public repository, or install a local package."
                )
            if response.status_code in {403, 429}:
                raise ValueError(
                    "GitHub is rate limiting this request. Keep the saved catalog and try refreshing later."
                )
            response.raise_for_status()
            revision = response.json()["sha"]
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("GitHub did not return an immutable commit.")
            data = bytearray()
            async with client.stream(
                "GET", f"https://codeload.github.com/{source}/zip/{revision}"
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 10_000_000:
                        raise ValueError("Marketplace download exceeds 10 MB.")
        return revision, unpack_archive(bytes(data))

    async def refresh(self, source, ref="HEAD"):
        revision, files = await self.repository(source, ref)
        return self.add_catalog(source, revision, files)

    def add_catalog(self, source, revision, files):
        catalog = json.loads(files.get("shinbot-marketplace.json", b"{}"))
        if catalog.get("schema_version") != 1 or not isinstance(catalog.get("plugins"), list):
            raise ValueError("Repository needs a version 1 shinbot-marketplace.json catalog.")
        if len(catalog["plugins"]) > 200:
            raise ValueError("A marketplace may contain at most 200 plugins.")
        packages = {}
        for item in catalog["plugins"]:
            prefix = str(relative_file(item["path"])).rstrip("/") + "/"
            package = {
                name[len(prefix) :]: content for name, content in files.items() if name.startswith(prefix)
            }
            manifest, digest = validate_package(package)
            if (manifest.id, manifest.version, digest) != (item["id"], item["version"], item["sha256"]):
                raise ValueError(f"Catalog identity or digest mismatch for {item['id']}.")
            key = f"{manifest.id}@{manifest.version}"
            if key in packages:
                raise ValueError("Duplicate plugin in catalog.")
            packages[key] = {name: base64.b64encode(content).decode() for name, content in package.items()}
        existing = self.db.one("SELECT id FROM marketplaces WHERE source=?", (source,))
        key = existing["id"] if existing else uid()
        snapshot = {"catalog": catalog, "packages": packages}
        self.db.execute(
            "INSERT INTO marketplaces VALUES (?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET "
            "revision=excluded.revision,catalog=excluded.catalog,updated=excluded.updated,error=NULL",
            (key, source, revision, packed(snapshot), time.time()),
        )
        return {"id": key, "source": source, "revision": revision, "plugins": catalog["plugins"]}

    def catalog(self):
        output = []
        for market in self.db.all("SELECT * FROM marketplaces ORDER BY source"):
            snapshot = json.loads(market.pop("catalog"))
            market["plugins"] = snapshot.get("catalog", {}).get("plugins", [])
            output.append(market)
        return output

    def stage_from_catalog(self, marketplace, plugin, version):
        row = self.db.one("SELECT * FROM marketplaces WHERE id=?", (marketplace,))
        if not row:
            raise ValueError("Marketplace not found.")
        files = json.loads(row["catalog"]).get("packages", {}).get(f"{plugin}@{version}")
        if not files:
            raise ValueError("Plugin version not found. Refresh its marketplace.")
        return self.stage(
            {name: base64.b64decode(content, validate=True) for name, content in files.items()}, marketplace
        )

    def stage(self, files, marketplace=None, *, learned=False):
        manifest, digest = validate_package(files)
        if (manifest.id.startswith("skill-learned-") or "LEARNING.json" in files) and not learned:
            raise ValueError(
                "Learned skill identities and evidence are reserved for ShinBot learning proposals."
            )
        if not learned and marketplace is None:
            from .sources import source_identity

            for previous in self.db.all("SELECT manifest FROM plugins WHERE id=?", (manifest.id,)):
                if source_identity(json.loads(previous["manifest"]).get("source")) != source_identity(
                    manifest.source
                ):
                    raise ValueError(
                        "A different extension already uses this ID. Choose a different name instead of replacing its versions or access."
                    )
        if marketplace is None:
            reserved = {item["id"] for catalog in self.catalog() for item in catalog["plugins"]}
            reserved.update(
                row["id"] for row in self.db.all("SELECT id FROM plugins WHERE marketplace IS NOT NULL")
            )
            if manifest.id in reserved:
                raise ValueError(
                    "This plugin ID belongs to a marketplace. Install it from that catalog, "
                    "or choose a different ID for your local package."
                )
        existing = self.db.one(
            "SELECT * FROM plugins WHERE id=? AND version=?", (manifest.id, manifest.version)
        )
        if existing:
            if existing["digest"] != digest:
                raise ValueError("This version already exists with different contents. Increase the version.")
            Security(self.db).scan(files)
            return self.public(existing)
        Security(self.db).scan(files)
        path = self.directory / manifest.id / manifest.version
        staging = self.directory / f"staging-{uid()}"
        staging.mkdir(parents=True)
        try:
            for name, content in files.items():
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            path.parent.mkdir(exist_ok=True)
            with self.db.transaction() as db:
                # Another scan can finish concurrently; version contents remain immutable.
                previous = db.execute(
                    "SELECT * FROM plugins WHERE id=? AND version=?", (manifest.id, manifest.version)
                ).fetchone()
                if previous:
                    if previous["digest"] != digest:
                        raise ValueError(
                            "This version already exists with different contents. Increase the version."
                        )
                else:
                    if path.exists():
                        entries = list(path.rglob("*"))
                        saved = {p.relative_to(path).as_posix(): p for p in entries if p.is_file()}
                        if (
                            path.is_symlink()
                            or any(p.is_symlink() for p in entries)
                            or set(saved) != set(files)
                            or any(
                                saved[n].stat().st_size != len(data) or saved[n].read_bytes() != data
                                for n, data in files.items()
                            )
                        ):
                            raise ValueError(
                                "Files from an interrupted installation differ from this package. Use a new version or inspect the plugin directory on the server."
                            )
                        # Resume a crash between the completed rename and database commit.
                    else:
                        staging.rename(path)
                    db.execute(
                        "INSERT INTO plugins VALUES (?,?,?,?,?,?,?,?)",
                        (
                            manifest.id,
                            manifest.version,
                            digest,
                            manifest.model_dump_json(),
                            str(path),
                            "staged",
                            marketplace,
                            time.time(),
                        ),
                    )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return self.public(
            self.db.one("SELECT * FROM plugins WHERE id=? AND version=?", (manifest.id, manifest.version))
        )

    def origin(self, row):
        market = self.db.one("SELECT source FROM marketplaces WHERE id=?", (row["marketplace"],))
        if market:
            return market["source"]
        if row["marketplace"]:
            return self.db.setting("marketplace-source:" + row["marketplace"], "Removed marketplace")
        return "Local package"

    def public(self, row):
        manifest = json.loads(row["manifest"])
        fields = (
            self.configuration(row["id"], row["version"])["fields"] if manifest.get("configuration") else []
        )
        return {
            **manifest,
            "missing_configuration": [f["label"] for f in fields if f["required"] and not f["configured"]],
            "status": row["status"],
            "source": self.db.setting(
                f"plugin-source:{row['id']}:{row['version']}", json.loads(row["manifest"]).get("source")
            ),
            "digest": row["digest"],
            "security": Security(self.db).report(row["digest"]),
            "marketplace": row["marketplace"],
            "origin": self.origin(row),
            "access": self.access(row["id"]),
            "execution_access": "Full workspace and outbound network access"
            if manifest["kind"] == "mcp"
            or manifest.get("hooks")
            or manifest.get("tools")
            and manifest["kind"] == "extension"
            else (
                "ShinBot work cards"
                if json.loads(row["manifest"])["kind"] == "builtin"
                else "Runs inside ShinBot and exchanges messages with the connected account"
                if json.loads(row["manifest"])["kind"] == "channel"
                else "Trusted JavaScript and styles in the signed-in app"
                if manifest.get("ui")
                else "No executable tools"
            ),
        }

    def ui_package(self, plugin, version, bot_id):
        if not self.db.one("SELECT id FROM bots WHERE id=? AND archived=0", (bot_id,)):
            raise ValueError("Select an existing bot for this plugin.")
        row, files = self.package(plugin, version)
        if row["status"] != "active" or not self.allowed(plugin, bot_id):
            raise ValueError("This plugin is disabled or unavailable to the selected bot.")
        manifest = json.loads(row["manifest"])
        if not manifest.get("ui"):
            raise ValueError("This plugin has no user interface.")
        return manifest, files

    def list(self):
        return [self.public(r) for r in self.db.all("SELECT * FROM plugins ORDER BY id,created DESC")]

    def versions(self, bot_id="default"):
        return [
            f"plugin:{r['id']}:{r['version']}"
            for r in self.db.all("SELECT * FROM plugins WHERE status='active'")
            if self.allowed(r["id"], bot_id)
            and json.loads(r["manifest"])["kind"] != "channel"
            and not self.public(r)["missing_configuration"]
        ]

    def access(self, plugin):
        return self.db.setting("plugin_access:" + plugin, {"mode": "shared", "bots": []})

    def set_access(self, plugin, access):
        if not self.db.one("SELECT id FROM plugins WHERE id=?", (plugin,)):
            raise NotFound("Plugin not installed.")
        access = self.validate_access(access)
        self.db.execute(
            "INSERT OR REPLACE INTO settings VALUES (?,?)", ("plugin_access:" + plugin, packed(access))
        )
        return access

    def validate_access(self, access):
        if not isinstance(access, dict) or set(access) != {"mode", "bots"}:
            raise ValueError("Choose shared access or selected bots.")
        mode, bots = access["mode"], access["bots"]
        if (
            mode not in {"shared", "bots"}
            or not isinstance(bots, list)
            or any(not isinstance(b, str) for b in bots)
        ):
            raise ValueError("Choose shared access or selected bots.")
        available = {row["id"] for row in self.db.all("SELECT id FROM bots WHERE archived=0")}
        if (mode == "shared" and bots) or (mode == "bots" and (not bots or not set(bots) <= available)):
            raise ValueError("Select at least one existing bot, or share with all bots.")
        return {"mode": mode, "bots": sorted(set(bots))}

    def allowed(self, plugin, bot_id):
        access = self.access(plugin)
        return access["mode"] == "shared" or bot_id in access["bots"]

    def configuration(self, plugin, version, changes=None, *, environment=False):
        row, _ = self.package(plugin, version)
        fields = json.loads(row["manifest"]).get("configuration", [])
        secret_keys = {
            field["key"]
            for installed in self.db.all("SELECT manifest FROM plugins WHERE id=?", (plugin,))
            for field in json.loads(installed["manifest"]).get("configuration", [])
            if field["secret"]
        }
        directory = self.directory.parent / "plugin-settings"
        # A locally generated replacement cannot inherit a marketplace's account.
        scope = row["marketplace"] or row["digest"]
        scope_hash = hashlib.sha256(scope.encode()).hexdigest()[:20]
        path = directory / f"{plugin}-{scope_hash}.json"
        vault_scope = "plugin:" + plugin + ":" + scope
        values = (
            self.vault.update(vault_scope, {}, legacy=path) if path.exists() else self.vault.read(vault_scope)
        )
        if changes is not None:
            if not isinstance(changes, dict) or set(changes) - {field["key"] for field in fields}:
                raise ValueError("Choose settings declared by this plugin version.")
            values = self.vault.update(vault_scope, changes)
        if environment:
            missing = [
                field["label"] for field in fields if field["required"] and not values.get(field["key"])
            ]
            if missing:
                raise ValueError("Open Plugins → Connection and set: " + ", ".join(missing) + ".")
            return {field["key"]: values[field["key"]] for field in fields if values.get(field["key"])}
        return {
            "name": json.loads(row["manifest"])["name"],
            "origin": self.origin(row),
            "fields": [
                {
                    **field,
                    "secret": field["key"] in secret_keys,
                    "configured": bool(values.get(field["key"])),
                    "value": "" if field["key"] in secret_keys else values.get(field["key"], ""),
                }
                for field in fields
            ],
        }

    def package(self, plugin, version):
        row = self.db.one("SELECT * FROM plugins WHERE id=? AND version=?", (plugin, version))
        if not row:
            raise NotFound("Plugin version not installed.")
        manifest = json.loads(row["manifest"])
        try:
            files = {
                name: (Path(row["path"]) / name).read_bytes()
                for name in manifest["files"] + ["shinbot-plugin.json"]
            }
        except OSError:
            # Reported like any other plugin fault, so a message that needs it fails instead of retrying.
            raise ValueError(
                f"The installed files of {manifest.get('name', plugin)} {version} are missing or unreadable. Reinstall it."
            ) from None
        # The bytes are read and hashed on every use; validating them is repeated only when they change.
        digest = content_digest(files)
        if digest not in self.validated:
            validate_package(files)
            self.validated.add(digest)
        if digest != row["digest"]:
            raise ValueError("Installed plugin contents changed. Reinstall the immutable artifact.")
        return row, files

    def activate(self, plugin, version):
        row, files = self.package(plugin, version)
        Security(self.db).require(files)
        self.configuration(plugin, version, environment=True)
        with self.db.transaction() as db:
            db.execute("UPDATE plugins SET status='draining' WHERE id=? AND status='active'", (plugin,))
            db.execute("UPDATE plugins SET status='active' WHERE id=? AND version=?", (plugin, version))
        self.db.event("plugin", f"Activated {plugin} {version}")

    def disable(self, plugin):
        self.db.execute("UPDATE plugins SET status='disabled' WHERE id=?", (plugin,))
