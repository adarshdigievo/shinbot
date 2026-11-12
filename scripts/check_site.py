"""Check rendered website links, assets, anchors and search destinations."""

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SITE_URL = "https://adarshdigievo.github.io/shinbot/"


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.anchors = set()
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.anchors.add(attrs["id"])
        if tag == "a" and attrs.get("name"):
            self.anchors.add(attrs["name"])
        # Canonical and social metadata describe production URLs, not fetches.
        for attr in ("href", "src", "poster"):
            if attrs.get(attr) and not (tag == "link" and attrs.get("rel") == "canonical"):
                self.links.append(attrs[attr])


def check(directory, site_url=SITE_URL):
    directory = directory.resolve()
    origin = urlsplit(site_url)
    pages = {p: Page(p.read_text()) for p in directory.rglob("*.html")}
    errors = []
    count = 0

    def verify(source, link):
        nonlocal count
        target = urlsplit(urljoin(site_url + source.relative_to(directory).as_posix(), link))
        if target.scheme not in ("https", "http") or target.netloc != origin.netloc:
            return
        count += 1
        if not target.path.startswith(origin.path):
            errors.append(f"{source.relative_to(directory)}: outside project prefix: {link}")
            return
        relative = unquote(target.path[len(origin.path) :])
        resolved = (directory / relative).resolve()
        if not resolved.is_relative_to(directory):
            errors.append(f"{source.relative_to(directory)}: outside site directory: {link}")
            return
        if resolved.is_dir():
            resolved /= "index.html"
        if not resolved.is_file():
            errors.append(f"{source.relative_to(directory)}: missing destination: {link}")
        elif target.fragment and resolved in pages:
            fragment = unquote(target.fragment)
            if fragment not in pages[resolved].anchors and not fragment.startswith(":~:text="):
                errors.append(f"{source.relative_to(directory)}: missing anchor: {link}")

    for path, page in pages.items():
        for link in page.links:
            verify(path, link)
    for path in directory.rglob("*.css"):
        for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", path.read_text()):
            verify(path, match[2])
    search = directory / "docs/search.json"
    if search.exists():
        for item in json.loads(search.read_text()):
            if item.get("href"):
                verify(search, item["href"])
    else:
        errors.append("Missing documentation search index.")
    for required in ("index.html", "docs/index.html", ".nojekyll"):
        if not (directory / required).is_file():
            errors.append(f"Missing site entry point: {required}")
    if errors:
        raise ValueError("\n".join(sorted(set(errors))))
    print(f"Verified {len(pages)} HTML pages and {count} local link, asset and search references.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    try:
        check(args.directory)
    except ValueError as exc:
        parser.exit(1, f"Website check failed:\n{exc}\n")


if __name__ == "__main__":
    main()
