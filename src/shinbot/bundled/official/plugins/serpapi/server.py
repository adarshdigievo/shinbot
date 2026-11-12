"""Bounded SerpApi tools. One request per call; keys stay in private configuration."""

import json
import logging
import os
import re
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SerpApi")
API = "https://serpapi.com/search.json"


class PrivateRequestLogs(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        key = os.getenv("SHINBOT_PLUGIN_SERPAPI_KEY", "").strip()
        if key:
            message = message.replace(key, "[redacted]")
        record.msg = re.sub(r"(?i)(api_key=)[^&\s\"']+", r"\1[redacted]", message)
        record.args = ()
        return True


logging.getLogger("httpx").addFilter(PrivateRequestLogs())
logging.getLogger("httpx").setLevel(logging.WARNING)


def request(parameters):
    key = os.getenv("SHINBOT_PLUGIN_SERPAPI_KEY", "").strip()
    if not key:
        raise ValueError(
            "Add your SerpApi key in Plugins → SerpApi → Configure, then activate the extension."
        )
    try:
        with httpx.Client(timeout=45, trust_env=False, follow_redirects=False) as client:
            with client.stream(
                "GET", API, params={**parameters, "api_key": key, "output": "json"}
            ) as response:
                if response.status_code >= 300:
                    errors = {
                        401: "Check the saved SerpApi key.",
                        403: "This SerpApi account cannot make this request.",
                        429: "SerpApi reached a usage or rate limit. Check your account before retrying.",
                    }
                    raise ValueError(
                        errors.get(
                            response.status_code,
                            f"SerpApi returned HTTP {response.status_code}. Check the engine parameters or try later.",
                        )
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 4_000_000:
                        raise ValueError("The SerpApi response exceeds 4 MB. Narrow the request.")
                result = json.loads(data)
    except httpx.HTTPError:
        raise ValueError(
            "SerpApi did not respond. The request may have used a search credit; check your account before retrying."
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("SerpApi returned an unreadable response. Try later.") from None
    if (
        not isinstance(result, dict)
        or result.get("error")
        or result.get("search_metadata", {}).get("status") != "Success"
    ):
        raise ValueError(
            "SerpApi could not complete this search. Check the engine's required parameters, account quota and whether the content is available."
        )
    return result


def bounded(value, limit, key, depth=0):
    if depth > 7:
        return "[nested data omitted]"
    if isinstance(value, str):
        value = value.replace(key, "[redacted]") if key else value
        return value[:2000]
    if isinstance(value, list):
        return [bounded(x, limit, key, depth + 1) for x in value[:limit]]
    if isinstance(value, dict):
        return {
            k: bounded(v, limit, key, depth + 1)
            for k, v in list(value.items())[:60]
            if k
            not in {"api_key", "search_parameters", "raw_html_file", "json_endpoint", "prettify_html_file"}
        }
    return value


@mcp.tool()
def search(engine: str, parameters: dict, limit: int = 5) -> dict:
    """Run one SerpApi search. Check official engine docs for parameters. Returns bounded JSON; never paginates automatically."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", engine) or not 1 <= limit <= 20:
        raise ValueError("Choose a SerpApi engine and a result limit between 1 and 20.")
    reserved = {"api_key", "engine", "output", "async", "no_cache", "json_restrictor"}
    if (
        not isinstance(parameters, dict)
        or len(parameters) > 30
        or any(
            k.lower() in reserved
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,59}", k)
            or not isinstance(v, (str, int, float, bool))
            or len(str(v)) > 2000
            for k, v in parameters.items()
        )
    ):
        raise ValueError(
            "Use engine-specific query parameters. Authentication, caching and response format are managed by ShinBot."
        )
    data = request({**parameters, "engine": engine})
    result = bounded(data, limit, os.getenv("SHINBOT_PLUGIN_SERPAPI_KEY", ""))
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > 30000:
        return {
            "search_id": data.get("search_metadata", {}).get("id"),
            "truncated": True,
            "text": text[:30000],
            "note": "Use a smaller result limit or a more specific query.",
        }
    return {
        "data": result,
        "result_limit": limit,
        "note": "Lists and long text are capped. Each call makes one request with caching enabled.",
    }


@mcp.tool()
def web_search(query: str, limit: int = 5, country: str = "us", language: str = "en") -> dict:
    """Search the web with Google Light. Returns titles, source links and snippets."""
    if (
        not query.strip()
        or len(query) > 2000
        or not re.fullmatch(r"[A-Za-z-]{2,12}", language)
        or not re.fullmatch(r"[A-Za-z]{2}", country)
    ):
        raise ValueError("Provide a query, two-letter country and language code such as en.")
    return search("google_light", {"q": query, "gl": country, "hl": language}, limit)


@mcp.tool()
def youtube_transcript(video: str, language_code: str = "en", start_ms: int = 0, limit: int = 100) -> dict:
    """Read a YouTube transcript with timestamps. Use next_start_ms for the next segment. Missing captions are reported as an error."""
    identifier = video
    if "://" in video:
        url = urlsplit(video)
        if url.scheme != "https" or url.username or url.password:
            raise ValueError("Use a public HTTPS YouTube URL or an 11-character video ID.")
        if url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            identifier = (
                parse_qs(url.query).get("v", [""])[0] if url.path == "/watch" else url.path.split("/")[-1]
            )
        elif url.hostname == "youtu.be":
            identifier = url.path.lstrip("/")
        else:
            raise ValueError("Use a YouTube URL or video ID.")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier)
        or not re.fullmatch(r"[A-Za-z-]{2,20}", language_code)
        or start_ms < 0
        or not 1 <= limit <= 200
    ):
        raise ValueError("Check the video ID, language, start time and segment limit (1–200).")
    data = request({"engine": "youtube_video_transcript", "v": identifier, "language_code": language_code})
    rows = [r for r in data.get("transcript", []) if r.get("start_ms", -1) >= start_ms]
    if not isinstance(data.get("transcript"), list) or not data["transcript"]:
        raise ValueError("This video has no available transcript for this request.")
    selected, size = [], 0
    for row in rows[:limit]:
        snippet = str(row.get("snippet", ""))
        if selected and size + len(snippet[:4000]) > 24000:
            break
        selected.append({**row, "snippet": snippet[:4000], "truncated": len(snippet) > 4000})
        size += len(snippet[:4000])
    return {
        "video": "https://www.youtube.com/watch?v=" + identifier,
        "search_id": data.get("search_metadata", {}).get("id"),
        "segments": [
            {k: r.get(k) for k in ("start_ms", "end_ms", "snippet", "start_time_text", "truncated")}
            for r in selected
        ],
        "available_transcripts": [
            {k: r.get(k) for k in ("title", "language_code", "selected")}
            for r in data.get("available_transcripts", [])[:30]
        ],
        "next_start_ms": rows[len(selected)]["start_ms"] if len(rows) > len(selected) else None,
    }


if __name__ == "__main__":
    mcp.run()
