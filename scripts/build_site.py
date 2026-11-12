"""Build Great Docs and assemble the GitHub Pages project site."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    # Stage a standalone docs project so section paths stay short in public URLs.
    for directory in (ROOT / "great-docs", ROOT / "_site"):
        if directory.exists():
            shutil.rmtree(directory)
    project = ROOT / "great-docs/project"
    project.mkdir(parents=True)
    for filename in ("great-docs.yml", "pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / filename, project / filename)
    for directory in ("user_guide", "developers", "reference", "assets"):
        shutil.copytree(ROOT / "docs" / directory, project / directory)
    for filename in ("mascot-light.png", "mascot-navy.png", "shinbot-icon.png", "favicon.png"):
        shutil.copy2(ROOT / "src/shinbot/web/brand" / filename, project / "assets" / filename)
    executable = Path(sys.executable).parent / "great-docs"
    subprocess.run([str(executable), "build", "--project-path", str(project)], cwd=ROOT, check=True)
    docs = project / "great-docs/_site"
    if not (docs / "index.html").is_file():
        raise SystemExit("Great Docs did not produce its documentation index.")
    output = ROOT / "_site"
    shutil.copytree(ROOT / "site", output)
    shutil.copytree(docs, output / "docs")
    (output / ".nojekyll").touch()
    # Great Docs' automatic LLM files depend on API discovery. These guides are
    # authored product documentation, so provide an index of the actual pages.
    index = ["# ShinBot", "", "Self-hosted personal assistant: setup, usage and developer guides.", ""]
    full = []
    for path in sorted((ROOT / "docs").glob("*/*.qmd")):
        section = path.parent.name.replace("_", "-")
        slug = re.sub(r"^\d+-", "", path.stem)
        route = (
            "index.html" if path.name == "index.qmd" and section == "user-guide" else f"{section}/{slug}.html"
        )
        if not (output / "docs" / route).is_file():
            raise SystemExit(f"Authored guide did not render: {path.relative_to(ROOT)}")
        content = path.read_text()
        title = re.search(r"(?m)^title: (.+)$", content)[1].strip('"')
        url = f"https://adarshdigievo.github.io/shinbot/docs/{route}"
        index.append(f"- [{title}]({url})")
        body = content.split("---", 2)[-1].strip()
        full.append(f"# {title}\n\nSource: {url}\n\n{body}")
    (output / "docs/llms.txt").write_text("\n".join(index) + "\n")
    (output / "docs/llms-full.txt").write_text("\n\n".join(full) + "\n")
    print(f"Website built at {output}; documentation is under /docs/.")


if __name__ == "__main__":
    main()
