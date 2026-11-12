"""Adapters for standard MCP connections and Agent Skills packages."""

import hashlib
import re
import shlex
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .plugins import PERMISSIVE, PluginManifest, relative_file


class MCPServer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list, max_length=100)
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict, max_length=20)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def validate_server(self):
        if self.transport not in {"stdio", "http", "sse"}:
            raise ValueError("Choose stdio, http or sse transport.")
        if self.transport == "stdio":
            if not self.command or len(self.command) > 1000 or "\x00" in self.command:
                raise ValueError("Enter the MCP executable, such as npx, uvx or python.")
            if self.url or self.headers:
                raise ValueError("Stdio servers use an executable and environment variables.")
        else:
            url = urlsplit(self.url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.fragment
                or url.query
            ):
                raise ValueError(
                    "Use an HTTP(S) endpoint without credentials, query parameters or fragments. Put tokens in headers."
                )
            if self.command or self.args or self.env:
                raise ValueError("Remote servers use a URL and optional headers.")
        if any(len(value) > 10000 or "\x00" in value for value in self.args):
            raise ValueError("MCP arguments must be text under 10,000 characters.")
        for key, value in {**self.env, **self.headers}.items():
            if (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", key)
                or len(value) > 10000
                or any(c in value for c in "\r\n\x00")
            ):
                raise ValueError("Use valid setting names and single-line values.")
        return self


def connection_package(plugin_id, name, version, server, tools):
    server = MCPServer.model_validate(server).model_dump()
    settings, values = [], {}
    for group in ("env", "headers"):
        for index, (key, value) in enumerate(server[group].items()):
            setting = f"SHINBOT_PLUGIN_MCP_{group.upper()}_{index}"
            settings.append({"key": setting, "label": key, "secret": True, "required": True})
            values[setting] = value
            server[group][key] = setting
    manifest = PluginManifest(
        id=plugin_id,
        name=name,
        version=version,
        kind="mcp",
        description="An MCP connection configured in ShinBot.",
        license="MIT",
        files=["LICENSE"],
        server=server,
        tools=tools,
        configuration=settings,
        permissions=["workspace", "network"],
    )
    # This notice covers the configuration adapter, not the connected server.
    files = {
        "shinbot-plugin.json": manifest.model_dump_json().encode(),
        "LICENSE": b'MIT License\n\nCopyright (c) 2026 ShinBot contributors\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\nof this software and associated documentation files (the "Software"), to deal\nin the Software without restriction, including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n',
    }
    return files, values


def skills_command(command):
    parts = shlex.split(command)
    if (
        len(parts) < 4
        or parts[:1] != ["npx"]
        or parts[1] not in {"skills", "skills@1.7.2"}
        or parts[2] not in {"add", "install"}
    ):
        raise ValueError("Use npx skills add SOURCE --skill NAME. ShinBot runs its pinned Skills CLI.")
    source = parts[3]
    if source.startswith("-") or not source.strip() or len(source) > 2000:
        raise ValueError("Enter a skill repository or local path.")
    names = []
    index = 4
    while index < len(parts):
        flag = parts[index]
        if flag in {"--yes", "-y", "--copy"}:
            index += 1
        elif flag in {"--skill", "-s"}:
            index += 1
            start = index
            while index < len(parts) and not parts[index].startswith("-"):
                names.append(parts[index])
                index += 1
            if index == start:
                raise ValueError("Give --skill a name.")
        else:
            raise ValueError(
                "Supported options: --skill NAME, --copy and --yes. Choose bot access in ShinBot."
            )
    return source, names


def skill_package(files, license_name, source="Local skill", *, learned=False):
    if license_name not in PERMISSIVE:
        raise ValueError("Check the skill's upstream license. This license needs a host review.")
    if "SKILL.md" not in files:
        raise ValueError("A skill directory must contain SKILL.md.")
    text = files["SKILL.md"].decode()
    frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not frontmatter:
        raise ValueError("SKILL.md needs YAML frontmatter with name and description.")
    try:
        metadata = yaml.safe_load(frontmatter.group(1))
    except yaml.YAMLError:
        raise ValueError("SKILL.md frontmatter must be valid YAML.") from None
    name = metadata.get("name") if isinstance(metadata, dict) else None
    desc = metadata.get("description") if isinstance(metadata, dict) else None
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name)
        or "--" in name
        or name.endswith("-")
    ):
        raise ValueError("Use a skill name of up to 64 lowercase letters, digits or single hyphens.")
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError("SKILL.md needs a text description.")
    if not learned and (name.startswith("learned-") or "LEARNING.json" in files):
        raise ValueError("Learned skill identities and evidence are reserved for ShinBot learning proposals.")
    plugin_id = "skill-" + name
    if len(plugin_id) > 64:
        plugin_id = "skill-" + name[:45] + "-" + hashlib.sha256(name.encode()).hexdigest()[:10]
    content = dict(files)
    for path in content:
        relative_file(path)
    from .sources import audit_notices, license_files

    detected, notices, _ = license_files(content)
    if detected != license_name or metadata.get("license", license_name) not in {
        license_name,
        f"{license_name} License",
    }:
        raise ValueError("The selected license does not match the skill's license notice or metadata.")
    audit_notices(content, license_name)
    for path, data in notices.items():
        content.setdefault(path, data)
    digest = hashlib.sha256(
        source.encode()
        + b"\0"
        + license_name.encode()
        + b"\0"
        + b"".join(key.encode() + b"\0" + hashlib.sha256(content[key]).digest() for key in sorted(content))
    ).hexdigest()
    manifest = PluginManifest(
        id=plugin_id,
        name=name,
        version="1.0.0-" + digest[:12],
        description=desc.strip()[:1000],
        kind="skill",
        license=license_name,
        files=sorted(content),
        source=source,
    )
    content["shinbot-plugin.json"] = manifest.model_dump_json().encode()
    return content
