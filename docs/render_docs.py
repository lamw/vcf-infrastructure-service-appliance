#!/usr/bin/env python3
"""Render the Markdown documentation pages into static HTML for local preview.

This intentionally supports only the small Markdown subset used by the VIS docs.
It keeps GitHub Pages preview lightweight without adding a Node/Ruby dependency.
"""

from __future__ import annotations

import html
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PAGES = [
    "user-guide.md",
    "services.md",
    "usage.md",
    "architecture.md",
    "build.md",
    "deploy.md",
    "develop.md",
]

NAV = """
<a href="services.html">Services</a>
<a href="https://github.com/lamw/vcf-infrastructure-service-appliance/releases">Download</a>
<a href="usage.html">Usage</a>
<a href="architecture.html">Architecture</a>
<a href="deploy.html">Deploy</a>
<a href="develop.html">Develop</a>
<a href="build.html">Build</a>
"""


def inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img src="\2" alt="\1" />', escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def slugify(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", inline(text)).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "section"


def render_markdown(markdown: str) -> tuple[str, str]:
    lines = markdown.splitlines()
    out: list[str] = []
    title = "VIS Documentation"
    in_ul = False
    in_ol = False
    in_code = False
    code_lang = ""
    code_lines: list[str] = []
    paragraph: list[str] = []
    table_headers: list[str] | None = None
    table_rows: list[list[str]] = []

    def close_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            out.append("<p>{}</p>".format(inline(" ".join(paragraph))))
            paragraph = []

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def close_table() -> None:
        nonlocal table_headers, table_rows
        if table_headers is None:
            return
        out.append("<table>")
        out.append("<thead><tr>{}</tr></thead>".format("".join("<th>{}</th>".format(inline(cell)) for cell in table_headers)))
        out.append("<tbody>")
        for row in table_rows:
            out.append("<tr>{}</tr>".format("".join("<td>{}</td>".format(inline(cell)) for cell in row)))
        out.append("</tbody></table>")
        table_headers = None
        table_rows = []

    i = 0
    while i < len(lines):
        line = lines[i]

        if in_code:
            if line.startswith("```"):
                code = html.escape("\n".join(code_lines))
                if code_lang == "mermaid":
                    out.append('<div class="mermaid">{}</div>'.format(code))
                else:
                    out.append("<pre><code>{}</code></pre>".format(code))
                code_lines = []
                in_code = False
                code_lang = ""
            else:
                code_lines.append(line)
            i += 1
            continue

        if line.startswith("```"):
            close_paragraph()
            close_lists()
            close_table()
            in_code = True
            code_lang = line.strip()[3:].strip().split()[0].lower() if line.strip()[3:].strip() else ""
            code_lines = []
            i += 1
            continue

        if not line.strip():
            close_paragraph()
            close_lists()
            close_table()
            i += 1
            continue

        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", lines[i + 1]):
            close_paragraph()
            close_lists()
            table_headers = table_row(line)
            i += 2
            continue

        if table_headers is not None and line.startswith("|"):
            table_rows.append(table_row(line))
            i += 1
            continue
        close_table()

        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            close_paragraph()
            close_lists()
            level = len(heading.group(1))
            text = heading.group(2)
            if level == 1:
                title = re.sub(r"<[^>]+>", "", inline(text))
            out.append('<h{level} id="{slug}">{text}</h{level}>'.format(level=level, slug=slugify(text), text=inline(text)))
            i += 1
            continue

        bullet = re.match(r"^[-*+]\s+(.+)$", line)
        if bullet:
            close_paragraph()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append("<li>{}</li>".format(inline(bullet.group(1))))
            i += 1
            continue

        ordered = re.match(r"^\d+\.\s+(.+)$", line)
        if ordered:
            close_paragraph()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append("<li>{}</li>".format(inline(ordered.group(1))))
            i += 1
            continue

        close_lists()
        paragraph.append(line.strip())
        i += 1

    close_paragraph()
    close_lists()
    close_table()
    return title, "\n".join(out)


def page_template(title: str, body: str) -> str:
    mermaid_script = ""
    if 'class="mermaid"' in body:
        mermaid_script = """
  <script type="module">
    import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs";
    mermaid.initialize({
      startOnLoad: true,
      theme: "dark",
      securityLevel: "strict",
      themeVariables: {
        background: "#0b1822",
        primaryColor: "#142536",
        primaryTextColor: "#f4f8fb",
        primaryBorderColor: "#345267",
        lineColor: "#59b2e0",
        secondaryColor: "#1a3042",
        tertiaryColor: "#0f1f2b"
      }
    });
  </script>
"""
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} · VIS</title>
  <link rel="stylesheet" href="docs.css" />
</head>
<body>
  <header class="site-header">
    <a class="brand" href="index.html">
      <div class="brand-mark">VIS</div>
      <div>
        <strong>VCF Infrastructure Services Appliance</strong>
        <span>WilliamLam.com</span>
      </div>
    </a>
    <nav aria-label="Documentation navigation">
      {nav}
    </nav>
  </header>
  <main>
    {body}
  </main>
  <footer class="doc-footer">VCF Infrastructure Services Appliance documentation.</footer>
  <script>
    (() => {{
      const main = document.querySelector("main");
      if (!main) {{
        return;
      }}

      const children = Array.from(main.children);
      const fixedHeader = [];
      const documentElements = [];
      let headerDone = false;

      children.forEach((element) => {{
        const isDocumentNav = element.tagName === "P" && element.querySelector('a[href="index.html"]');
        if (!headerDone && (element.tagName === "H1" || isDocumentNav)) {{
          fixedHeader.push(element);
        }} else {{
          headerDone = true;
          documentElements.push(element);
        }}
      }});

      const filtered = [];
      let skipNextTocList = false;
      documentElements.forEach((element) => {{
        if (element.matches("h2#table-of-contents")) {{
          skipNextTocList = true;
          return;
        }}
        if (skipNextTocList && (element.tagName === "UL" || element.tagName === "OL")) {{
          skipNextTocList = false;
          return;
        }}
        skipNextTocList = false;
        filtered.push(element);
      }});

      const firstHeadingIndex = filtered.findIndex((element) => element.tagName === "H2");
      const introElements = firstHeadingIndex > 0 ? filtered.slice(0, firstHeadingIndex) : [];
      const sectionElements = firstHeadingIndex >= 0 ? filtered.slice(firstHeadingIndex) : filtered;
      const headings = sectionElements.filter((element) => element.tagName === "H2");
      if (!headings.length) {{
        return;
      }}

      main.replaceChildren(...fixedHeader, ...introElements);

      const shell = document.createElement("div");
      shell.className = "doc-shell";
      const nav = document.createElement("aside");
      nav.className = "doc-section-nav";
      nav.setAttribute("aria-label", "Page sections");
      const content = document.createElement("div");
      content.className = "doc-section-content";
      shell.append(nav, content);
      main.append(shell);

      const panes = [];
      let activePane = null;
      let activeEntry = null;

      function createPane(id, title) {{
        const navGroup = document.createElement("div");
        navGroup.className = "doc-section-nav-group";
        const pane = document.createElement("section");
        pane.className = "doc-section-pane";
        pane.dataset.sectionId = id;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "doc-section-button";
        button.textContent = title;
        button.dataset.sectionTarget = id;
        button.addEventListener("click", () => activate(id, true));
        const subnav = document.createElement("div");
        subnav.className = "doc-subsection-nav";
        navGroup.append(button, subnav);
        nav.append(navGroup);
        content.append(pane);
        panes.push({{ id, pane, button, subnav, subsectionButtons: [] }});
        return pane;
      }}

      function clearSubsectionState() {{
        panes.forEach((entry) => {{
          entry.subsectionButtons.forEach((button) => button.classList.remove("is-active"));
        }});
      }}

      function activate(id, updateHash) {{
        const target = panes.find((entry) => entry.id === id) || panes[0];
        panes.forEach((entry) => {{
          const active = entry === target;
          entry.pane.classList.toggle("is-active", active);
          entry.button.classList.toggle("is-active", active);
          entry.button.setAttribute("aria-pressed", active ? "true" : "false");
        }});
        clearSubsectionState();
        if (updateHash && target) {{
          history.replaceState(null, "", "#" + target.id);
        }}
      }}

      function addSubsection(entry, heading) {{
        const button = document.createElement("button");
        button.type = "button";
        button.className = "doc-subsection-button";
        button.textContent = heading.textContent.trim();
        button.dataset.sectionTarget = heading.id;
        button.addEventListener("click", () => {{
          activate(entry.id, false);
          clearSubsectionState();
          button.classList.add("is-active");
          history.replaceState(null, "", "#" + heading.id);
          requestAnimationFrame(() => heading.scrollIntoView({{ block: "start" }}));
        }});
        entry.subnav.append(button);
        entry.subsectionButtons.push(button);
      }}

      sectionElements.forEach((element) => {{
        if (element.tagName === "H2") {{
          activePane = createPane(element.id, element.textContent.trim());
          activeEntry = panes[panes.length - 1];
          activePane.append(element);
          return;
        }}
        if (element.tagName === "H3" && activeEntry) {{
          addSubsection(activeEntry, element);
        }}
        if (activePane) {{
          activePane.append(element);
        }}
      }});

      const requested = decodeURIComponent(location.hash.replace(/^#/, ""));
      const requestedParent = panes.find((entry) => entry.id === requested);
      const requestedChild = panes
        .flatMap((entry) => entry.subsectionButtons.map((button) => ({{ entry, button }})))
        .find((item) => item.button.dataset.sectionTarget === requested);
      if (requestedChild) {{
        activate(requestedChild.entry.id, false);
        requestedChild.button.classList.add("is-active");
        const target = document.getElementById(requested);
        if (target) {{
          requestAnimationFrame(() => target.scrollIntoView({{ block: "start" }}));
        }}
      }} else {{
        activate((requestedParent && requestedParent.id) || panes[0].id, false);
      }}
    }})();
  </script>
  {mermaid_script}
</body>
</html>
""".format(title=html.escape(title), nav=NAV, body=body, mermaid_script=mermaid_script)


def main() -> None:
    for page in PAGES:
        source = ROOT / page
        title, body = render_markdown(source.read_text(encoding="utf-8"))
        target = source.with_suffix(".html")
        target.write_text(page_template(title, body), encoding="utf-8")
        print("rendered {}".format(target.name))


if __name__ == "__main__":
    main()
