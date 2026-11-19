"""Classify browser interactions from the live target, independently of agent claims."""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import time
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

RISKY = re.compile(
    r"\b(delete|remove|erase|purge|destroy|drop|reset|revoke|transfer|pay|purchase|buy|checkout|"
    r"send|submit|publish|post|merge|deploy|unsubscribe|logout|sign\s*out|confirm|authorize|grant)\b",
    re.I,
)
# Common in headlines and article slugs, so these verbs only count in a short phrase.
ACTION_WORDS = (
    "cancel|close|approve|accept|decline|reject|disable|deactivate|terminate|withdraw|donate|refund|"
    "redeem|renew|subscribe|upgrade|downgrade|invite|ban"
)
ACTIONS = re.compile(rf"\b({ACTION_WORDS}|order|share|block)\b", re.I)
# Order, share and block are everyday nouns in page addresses.
PATH_ACTIONS = re.compile(rf"\b({ACTION_WORDS})\b", re.I)
PUNCTUATION = r"[_/=&?+.-]"
LOCAL_SUFFIXES = (".internal", ".local", ".localhost", ".lan", ".home.arpa")
resolved = {}


def words(text):
    """Split URL punctuation, camelCase and digit runs, so deleteAccount and pay2 read as words."""
    text = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])",
        " ",
        unquote(text),
    )
    return re.sub(PUNCTUATION, " ", text)


def risky(text, limit, actions=ACTIONS):
    return bool(RISKY.search(text) or (len(text.split()) <= limit and actions.search(text)))


def consequential(url):
    parsed = urlparse(url)
    pairs = [
        pair
        for pair in parse_qsl(parsed.query, keep_blank_values=True)
        if pair[0] not in {"q", "query", "search", "search_query"}
    ]
    # A fragment is a heading anchor unless it is a client-side route such as #/items/5/delete.
    route = parsed.fragment.split(":~:")[0] if parsed.fragment.startswith(("/", "!/")) else ""
    parts = [*parsed.path.split("/"), *route.split("/"), *(item for pair in pairs for item in pair)]
    whole = " ".join([parsed.path, *(f"{key} {value}" for key, value in pairs), route])
    return bool(RISKY.search(words(whole)) or any(risky(words(part), 3, PATH_ACTIONS) for part in parts))


def navigation_safe(url):
    parsed = urlparse(url)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not consequential(url)
    )


async def resolve(host):
    loop = asyncio.get_running_loop()
    return [item[4][0] for item in await asyncio.wait_for(loop.getaddrinfo(host, None), 3)]


def public_address(value):
    address = ipaddress.ip_address(value.split("%")[0])
    address = getattr(address, "ipv4_mapped", None) or address
    return address.is_global and not address.is_multicast


def endpoint(url):
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    return (parsed.hostname or "").lower().rstrip("."), port or {"http": 80, "https": 443}.get(parsed.scheme)


async def public_destination(url, current=""):
    """Auto reaches globally routable hosts, hosts the owner listed, and the origin that is already open."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return True  # Not a network address; navigation_safe rejects these as destinations.
    host, port = endpoint(url)
    trusted = {
        item.lower() for item in re.split(r"[\s,]+", os.getenv("SHINBOT_BROWSER_TRUSTED_HOSTS", "")) if item
    }
    if (host, port) == endpoint(current) or {host, f"{host}:{port}"} & trusted:
        return True
    try:
        return public_address(host)
    except ValueError:
        pass
    # Single-label names are Compose services and LAN machines.
    if "." not in host or host.endswith(LOCAL_SUFFIXES):
        return False
    now = time.monotonic()
    if host in resolved and resolved[host][0] > now:
        return resolved[host][1]
    try:
        addresses = await resolve(host)
        public = bool(addresses) and all(public_address(address) for address in addresses)
    except (OSError, ValueError):
        public = False  # A name that cannot be checked is the owner's decision, not Auto's.
    if len(resolved) > 256:
        resolved.clear()
    resolved[host] = (now + 60, public)
    return public


def classify(args, target):
    operation = args.get("operation")
    if operation in {"observe", "tabs", "screenshot"}:
        return True, "Read the current page."
    if operation == "close":
        return True, "Close this bot’s browser and keep its saved profile."
    public = target.get("public") is True
    local = "This address is on a private or local network. Review it before continuing."
    if operation == "goto":
        if not navigation_safe(args.get("url", "")):
            return False, "This address may trigger an account change or another consequential action."
        return (True, "Open a web page.") if public else (False, local)
    label = " ".join(str(target.get(k, "")) for k in ("label", "name", "href", "action"))
    if (
        RISKY.search(re.sub(PUNCTUATION, " ", unquote(label)))
        or risky(re.sub(PUNCTUATION, " ", str(target.get("label", ""))), 4)
        or risky(words(f"{target.get('name', '')} {target.get('method_hint', '')}"), 3)
        or any(consequential(str(target.get(k, ""))) for k in ("href", "action"))
    ):
        return False, "This control may change or delete data, send something, or confirm a transaction."
    action = urljoin(target.get("url", ""), target.get("action", ""))
    search = (
        target.get("method", "").lower() == "get"
        and target.get("search_form")
        and urlparse(action).netloc == urlparse(target.get("url", "")).netloc
        and re.fullmatch(r"/(?:search|s|results)/?", urlparse(action).path)
        and navigation_safe(action)
        and not target.get("sensitive_form")
    )
    if operation == "fill" and search and target.get("search_input"):
        return True, "Enter a search query."
    if operation == "press":
        if target.get("tag") in {"body", "html"} and args.get("key") in {
            "Tab",
            "Shift+Tab",
            "Escape",
            "PageDown",
            "PageUp",
            "ArrowDown",
            "ArrowUp",
        }:
            return True, "Move around the page."
        if args.get("key") == "Enter" and search and target.get("search_input"):
            return True, "Run a search."
    if operation == "click":
        if search and target.get("tag") in {"button", "input"} and target.get("type") == "submit":
            return True, "Run a search."
        if target.get("tag") == "a" and not target.get("download") and not target.get("inline_handler"):
            href, written = target.get("href", ""), target.get("raw_href")
            written = href if written is None else str(written).strip()
            # "#", an empty href and script-driven methods mark an anchor used as a button.
            plain = (
                written not in {"", "#", "#!"}
                and not written.lower().startswith("javascript:")
                and str(target.get("method_hint") or "get").lower() == "get"
                and not target.get("confirm")
                and target.get("role") in {None, "", "link"}
            )
            if plain and href and navigation_safe(href):
                return (True, "Follow a page link.") if public else (False, local)
        if (
            not target.get("form")
            and not target.get("inline_handler")
            and (target.get("role") == "tab" or target.get("expanded") in {"true", "false"})
        ):
            return True, "Open a tab or expand a section."
    return False, "ShinBot could not verify that this action is harmless. Review it before continuing."


async def assess(page, args):
    target = {"url": page.url}
    operation = args.get("operation")
    if operation in {"click", "fill", "press"}:
        locator = page.locator(args.get("selector", "body"))
        if await locator.count() != 1:
            raise ValueError("Observe again and select exactly one element before acting.")
        target = await locator.evaluate("""element => {
          const form=element.form || element.closest('form');
          const fields=form ? [...form.elements] : [];
          const query=field=>['search'].includes(field.type) || ['q','query','search','search_query'].includes(field.name) || field.getAttribute('role')==='searchbox';
          return {url:location.href,tag:element.tagName.toLowerCase(),role:element.getAttribute('role'),
            label:[element.getAttribute('aria-label'),element.getAttribute('title'),element.innerText,element.type==='submit'?element.value:''].filter(Boolean).join(' ').slice(0,500),
            name:element.name||'',type:element.type||'',href:element.href||'',raw_href:element.getAttribute('href'),download:element.hasAttribute('download'),
            method_hint:element.getAttribute('data-method')||element.getAttribute('data-turbo-method')||'',
            confirm:element.hasAttribute('data-confirm')||element.hasAttribute('data-turbo-confirm'),
            inline_handler:['onclick','onmousedown','onmouseup','onpointerdown','onpointerup'].some(name=>element.hasAttribute(name)),
            expanded:element.getAttribute('aria-expanded'),
            form:!!form,action:element.hasAttribute('formaction')?element.formAction:(form?.action||''),method:element.getAttribute('formmethod')||form?.method||'',
            search_form:!!form && fields.some(query),search_input:query(element),
            sensitive_form:fields.some(field=>field.type==='password'||/cc-|password|one-time-code/.test(field.autocomplete||'')),
            surrounding:(element.closest('form,tr,li,[role="dialog"],article')||element.parentElement)?.innerText?.slice(0,2000)||'',
            form_state:fields.slice(0,100).map(field=>[field.name,field.type,field.value?.slice(0,500),field.checked])};
        }""")
    destination = str(
        args.get("url", "")
        if operation == "goto"
        else target.get("href") or urljoin(target["url"], target.get("action", ""))
    )
    target["public"] = operation in {"goto", "click", "fill", "press"} and await public_destination(
        destination, target["url"]
    )
    allowed, reason = classify(args, target)
    context = hashlib.sha256(
        json.dumps({"target": target, "args": args}, sort_keys=True).encode()
    ).hexdigest()
    # The destination lets the executor tell an approved private address from a surprise redirect.
    return {"safe": allowed, "reason": reason, "context": context, "destination": destination}
