"""Classify browser interactions from the live target, independently of agent claims."""

import hashlib
import json
import re
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

RISKY = re.compile(
    r"\b(delete|remove|erase|purge|destroy|drop|reset|revoke|transfer|pay|purchase|buy|checkout|"
    r"send|submit|publish|post|merge|deploy|unsubscribe|logout|sign\s*out|confirm|authorize|grant)\b",
    re.I,
)


def navigation_safe(url):
    parsed = urlparse(url)
    query = " ".join(
        f"{key} {value}"
        for key, value in parse_qsl(parsed.query)
        if key not in {"q", "query", "search", "search_query"}
    )
    words = re.sub(r"[_/=&?+.-]", " ", unquote(parsed.path + " " + query))
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not RISKY.search(words)
    )


def classify(args, target):
    operation = args.get("operation")
    if operation in {"observe", "tabs", "screenshot"}:
        return True, "Read the current page."
    if operation == "close":
        return True, "Close this bot’s browser and keep its saved profile."
    if operation == "goto":
        if navigation_safe(args.get("url", "")):
            return True, "Open a web page."
        return False, "This address may trigger an account change or another consequential action."
    label = " ".join(str(target.get(k, "")) for k in ("label", "name", "href", "action"))
    if RISKY.search(re.sub(r"[_/=&?+.-]", " ", unquote(label))):
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
            href = target.get("href", "")
            if href and navigation_safe(href):
                return True, "Follow a page link."
        if (
            not target.get("form")
            and not target.get("inline_handler")
            and (target.get("role") == "tab" or target.get("expanded") in {"true", "false"})
        ):
            return True, "Open a tab or expand a section."
    return False, "ShinBot could not verify that this action is harmless. Review it before continuing."


async def assess(page, args):
    target = {"url": page.url}
    if args.get("operation") in {"click", "fill", "press"}:
        locator = page.locator(args.get("selector", "body"))
        if await locator.count() != 1:
            raise ValueError("Observe again and select exactly one element before acting.")
        target = await locator.evaluate("""element => {
          const form=element.form || element.closest('form');
          const fields=form ? [...form.elements] : [];
          const query=field=>['search'].includes(field.type) || ['q','query','search','search_query'].includes(field.name) || field.getAttribute('role')==='searchbox';
          return {url:location.href,tag:element.tagName.toLowerCase(),role:element.getAttribute('role'),
            label:[element.getAttribute('aria-label'),element.innerText,element.type==='submit'?element.value:''].filter(Boolean).join(' ').slice(0,500),
            name:element.name||'',type:element.type||'',href:element.href||'',download:element.hasAttribute('download'),
            inline_handler:element.hasAttribute('onclick'),expanded:element.getAttribute('aria-expanded'),
            form:!!form,action:element.hasAttribute('formaction')?element.formAction:(form?.action||''),method:element.getAttribute('formmethod')||form?.method||'',
            search_form:!!form && fields.some(query),search_input:query(element),
            sensitive_form:fields.some(field=>field.type==='password'||/cc-|password|one-time-code/.test(field.autocomplete||'')),
            surrounding:(element.closest('form,tr,li,[role="dialog"],article')||element.parentElement)?.innerText?.slice(0,2000)||'',
            form_state:fields.slice(0,100).map(field=>[field.name,field.type,field.value?.slice(0,500),field.checked])};
        }""")
    allowed, reason = classify(args, target)
    context = hashlib.sha256(
        json.dumps({"target": target, "args": args}, sort_keys=True).encode()
    ).hexdigest()
    return {"safe": allowed, "reason": reason, "context": context}
