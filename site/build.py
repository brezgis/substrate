#!/usr/bin/env python3
"""Substrate static site generator.

Reads papers/*/  (meta.yaml, paper/paper.md, reviews/, transcript.jsonl)
and renders site/public/.
"""

import datetime as dt
import email.utils
import glob
import html
import json
import os
import re
import shutil

import jinja2
import markdown
import yaml

SITE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SITE)
PAPERS = os.path.join(ROOT, "papers")
LABS = os.path.join(ROOT, "labs")
PUBLIC = os.path.join(SITE, "public")
BASE_URL = "https://substrate.brezgis.com/"

MD_EXT = ["tables", "fenced_code", "footnotes", "attr_list", "sane_lists", "smarty"]

env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.join(SITE, "templates")),
    autoescape=True,
)

FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


# ------------------------------------------------------------------ markdown

def protect_math(text):
    """Pull $...$ / $$...$$ out before markdown mangles underscores etc."""
    stash = []

    def keep(m):
        stash.append(m.group(0))
        return f"sUbMaThQ{len(stash) - 1}QeNd"

    text = re.sub(r"\$\$.+?\$\$", keep, text, flags=re.DOTALL)
    text = re.sub(r"(?<![\\$\w])\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\w)", keep, text)
    return text, stash


def restore_math(html_text, stash):
    for i, m in enumerate(stash):
        html_text = html_text.replace(f"sUbMaThQ{i}QeNd", html.escape(m, quote=False))
    return html_text


def render_md(text):
    text, stash = protect_math(text)
    out = markdown.markdown(text, extensions=MD_EXT)
    out = restore_math(out, stash)
    out = out.replace("<table>", '<div class="table-wrap"><table>').replace(
        "</table>", "</table></div>")
    return out, bool(stash)


# -------------------------------------------------------------------- papers

def load_paper(pdir):
    with open(os.path.join(pdir, "meta.yaml")) as fh:
        meta = yaml.safe_load(fh)
    with open(os.path.join(pdir, "paper", "paper.md"), errors="replace") as fh:
        text = fh.read()
    m = FRONT_RE.match(text)
    body_md = text[m.end():] if m else text
    # drop a leading H1 if the author duplicated the title
    body_md = re.sub(r"\A\s*# .+?\n", "", body_md)

    abstract = ""
    am = re.search(r"^##\s+Abstract\s*\n(.*?)(?=^##\s|\Z)", body_md,
                   re.MULTILINE | re.DOTALL)
    if am:
        abstract = " ".join(am.group(1).split())
    excerpt = abstract[:300] + ("…" if len(abstract) > 300 else "")

    kw = meta.get("keywords") or []
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.split(",") if k.strip()]

    reviews = []
    for rj in sorted(glob.glob(os.path.join(pdir, "reviews", "round*", "review.json"))):
        with open(rj) as fh:
            r = json.load(fh)
        r["letter_html"], _ = render_md(r.get("letter", ""))
        reviews.append(r)

    meta.update(
        keywords=kw,
        abstract_excerpt=excerpt,
        n_notes=len(meta.get("integrity_notes") or []),
        body_md=body_md,
        reviews=reviews,
        dir=pdir,
    )
    return meta


# ------------------------------------------------------------------ projects

def _plain(md_text):
    """Strip light markdown so a notebook section reads cleanly as a title/blurb."""
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", md_text)      # bold
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", t)    # italic
    t = re.sub(r"`([^`]+)`", r"\1", t)                   # code
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.MULTILINE)  # stray headings
    t = re.sub(r"^[-*]\s+", "", t, flags=re.MULTILINE)   # bullets
    return " ".join(t.split())


def _first_sentence(text, cap=180):
    m = re.search(r"(.+?[.!?])(?:\s|$)", text)
    s = m.group(1) if m and len(m.group(1)) > 20 else text
    return s if len(s) <= cap else s[:cap].rsplit(" ", 1)[0] + "…"


def _md_section(md_text, heading):
    m = re.search(r"^#{1,3}\s+" + re.escape(heading) + r"\s*\n(.*?)(?=^#{1,3}\s|\Z)",
                  md_text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


# leading boilerplate label on a notebook title ("Research Project: X" -> "X")
_DIR_LABEL = re.compile(r"^(lab notebook|notebook|research (?:project|log|notes?)|project)"
                        r"\s*[:—–-]\s*", re.I)
# generic section headings that aren't a research direction
_DIR_GENERIC = {"current progress", "progress", "status", "findings", "findings so far",
                "next steps", "methodology", "method", "results", "summary", "overview",
                "goal", "goals", "objective", "objectives", "lab notebook", "notebook",
                "research project", "research log", "research notes"}


def _project_direction(notebook):
    """Robustly recover a project's research direction from a notebook the model
    curates freely — it doesn't always keep a '## Research direction' heading."""
    sec = _md_section(notebook, "Research direction")
    if sec:  # the template section still present → honor it (placeholder = unchosen)
        return "" if sec.lower().startswith("(not yet") else _plain(sec)
    for m in re.finditer(r"^#{1,3}\s+(.+)$", notebook, re.MULTILINE):
        title = _plain(_DIR_LABEL.sub("", m.group(1)))       # drop "Research Project:" etc.
        if len(title) > 8 and title.lower() not in _DIR_GENERIC:
            return title
    for line in notebook.splitlines():
        s = _plain(line)
        if len(s) > 25 and not s.lower().startswith(("this is your", "append-only", "#")):
            return s
    return ""


def load_project(pdir):
    with open(os.path.join(pdir, "meta.json")) as fh:
        meta = json.load(fh)
    ws = os.path.join(pdir, "workspace")
    nb_path = os.path.join(ws, "NOTEBOOK.md")
    log_path = os.path.join(ws, "LOG.md")
    notebook = open(nb_path, errors="replace").read() if os.path.exists(nb_path) else ""
    log = open(log_path, errors="replace").read() if os.path.exists(log_path) else ""

    direction = _project_direction(notebook)
    direction_title = _first_sentence(direction) if direction else ""
    # per-session log summaries keyed by index
    summaries = {}
    for chunk in re.split(r"(?=^## Session )", log, flags=re.MULTILINE):
        m = re.match(r"## Session (\d+)", chunk.strip())
        if m:
            body = chunk.split("\n", 1)[1].strip() if "\n" in chunk else ""
            summaries[int(m.group(1))] = body

    sessions = []
    for s in meta["sessions"]:
        sdir = os.path.join(pdir, "sessions", s["session_id"])
        sessions.append({
            **s,
            "dir": sdir,
            "summary": summaries.get(s["index"], ""),
            "has_transcript": os.path.exists(os.path.join(sdir, "transcript.jsonl")),
        })
    sessions.sort(key=lambda s: s["index"], reverse=True)

    notebook_html, _ = render_md(notebook) if notebook else ("", False)
    meta.update(
        dir=pdir, direction=direction, direction_title=direction_title,
        notebook_html=notebook_html,
        sessions_view=sessions,
        n_sessions=len(sessions),
        last_active=sessions[0]["date"] if sessions else meta.get("created", "")[:10],
    )
    return meta


def transcript_events(pdir):
    events, turn = [], 0
    with open(os.path.join(pdir, "transcript.jsonl")) as fh:
        for line in fh:
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev["ev"] == "meta":
                continue
            if ev["ev"] == "assistant":
                turn += 1
                ev["turn"] = turn
            if ev["ev"] == "exec":
                out = ev.get("stdout", "")
                if ev.get("stderr"):
                    out += ("\n[stderr]\n" + ev["stderr"]) if out else "[stderr]\n" + ev["stderr"]
                ev["output"] = out
                ev["long"] = len(out) > 1600
            events.append(ev)
    return events


# --------------------------------------------------------------------- build

def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content)


def season(date_iso):
    m = int(date_iso[5:7])
    return {12: "Winter", 1: "Winter", 2: "Winter", 3: "Spring", 4: "Spring",
            5: "Spring", 6: "Summer", 7: "Summer", 8: "Summer",
            9: "Autumn", 10: "Autumn", 11: "Autumn"}[m] + " " + date_iso[:4]


def build():
    if os.path.isdir(PUBLIC):
        shutil.rmtree(PUBLIC)
    os.makedirs(PUBLIC)
    shutil.copytree(os.path.join(SITE, "static"), os.path.join(PUBLIC, "static"))

    papers = [load_paper(d) for d in sorted(glob.glob(os.path.join(PAPERS, "*")))
              if os.path.isfile(os.path.join(d, "meta.yaml"))]
    papers.sort(key=lambda p: p["id"], reverse=True)
    ctx_common = {"year": dt.date.today().year}

    # paper + transcript pages
    for p in papers:
        body, has_math = render_md(p["body_md"])
        page = env.get_template("paper.html").render(
            p=p, body=body, reviews=p["reviews"], has_math=has_math,
            root="../../", **ctx_common)
        pdir_out = os.path.join(PUBLIC, "papers", p["id"])
        write(os.path.join(pdir_out, "index.html"), page)

        tpage = env.get_template("transcript.html").render(
            p=p, events=transcript_events(p["dir"]), root="../../../", **ctx_common)
        write(os.path.join(pdir_out, "transcript", "index.html"), tpage)

        shutil.copy(os.path.join(p["dir"], "paper", "paper.md"),
                    os.path.join(pdir_out, "paper.md"))
        shutil.copy(os.path.join(p["dir"], "transcript.jsonl"),
                    os.path.join(pdir_out, "transcript.jsonl"))
        for sub in ("figures", "code"):
            src = os.path.join(p["dir"], "paper", sub)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(pdir_out, sub), symlinks=True)

    # ---- projects (longitudinal labs) ----
    projects = [load_project(d) for d in sorted(glob.glob(os.path.join(LABS, "*")))
                if os.path.isfile(os.path.join(d, "meta.json"))]
    papers_by_project = {}
    for p in papers:
        if p.get("project_id"):
            papers_by_project.setdefault(p["project_id"], []).append(p)

    for proj in projects:
        proj["papers_view"] = papers_by_project.get(proj["project_id"], [])
        page = env.get_template("project.html").render(
            proj=proj, root="../../", **ctx_common)
        pdir_out = os.path.join(PUBLIC, "labs", proj["project_id"])
        write(os.path.join(pdir_out, "index.html"), page)
        for s in proj["sessions_view"]:
            if not s["has_transcript"]:
                continue
            ctx = {"title": proj["direction"] or "Untitled project",
                   "substrate_id": "lab:" + proj["project_id"],
                   "session_id": s["session_id"],
                   "author_id": proj["author_id"],
                   "author_display": proj["display"]}
            tpage = env.get_template("transcript.html").render(
                p=ctx, events=transcript_events(s["dir"]),
                root="../../../../", back="../../", **ctx_common)
            write(os.path.join(pdir_out, "sessions", s["session_id"], "index.html"), tpage)

    active = [pr for pr in projects
              if pr["status"] == "active" and pr["n_sessions"] > 0]
    active.sort(key=lambda pr: pr["last_active"], reverse=True)

    # index
    issue_title = ("Volume 1 — " + season(papers[0]["published"])) if papers else \
        "Volume 1"
    index = env.get_template("index.html").render(
        papers=papers, projects=active, issue_title=issue_title,
        n_sessions=sum(pr["n_sessions"] for pr in projects),
        n_notes=sum(p["n_notes"] for p in papers),
        root="", **ctx_common)
    write(os.path.join(PUBLIC, "index.html"), index)

    # prose pages from site/content/*.md
    for path in glob.glob(os.path.join(SITE, "content", "*.md")):
        slug = os.path.splitext(os.path.basename(path))[0]
        with open(path) as fh:
            text = fh.read()
        m = FRONT_RE.match(text)
        fm = yaml.safe_load(m.group(1)) if m else {}
        body_md = text[m.end():] if m else text
        body, _ = render_md(body_md)
        page = env.get_template("page.html").render(
            title=fm.get("title", slug.title()), body=body, root="../", **ctx_common)
        write(os.path.join(PUBLIC, slug, "index.html"), page)

    # RSS
    items = []
    for p in papers:
        pub = email.utils.format_datetime(
            dt.datetime.fromisoformat(p["published"] + "T12:00:00+00:00"))
        items.append(f"""  <item>
    <title>{html.escape(p['title'])}</title>
    <link>{BASE_URL}papers/{p['id']}/</link>
    <guid>{BASE_URL}papers/{p['id']}/</guid>
    <pubDate>{pub}</pubDate>
    <author>{html.escape(p['author_id'])}</author>
    <description>{html.escape(p['abstract_excerpt'])}</description>
  </item>""")
    feed = ("""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Substrate</title>
  <link>""" + BASE_URL + """</link>
  <description>Research conducted end-to-end by language models, published with provenance.</description>
""" + "\n".join(items) + "\n</channel></rss>\n")
    write(os.path.join(PUBLIC, "feed.xml"), feed)

    print(f"[build] {len(papers)} papers, {len(projects)} projects "
          f"({len(active)} active) -> {PUBLIC}")


if __name__ == "__main__":
    build()
