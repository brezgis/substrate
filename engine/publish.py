"""Publication: move an accepted submission into papers/ with full record."""

import datetime as dt
import glob
import json
import os
import re
import shutil

import yaml

from . import harness

ROOT = harness.ROOT
PAPERS_DIR = os.path.join(ROOT, "papers")


def next_paper_id():
    today = dt.date.today()
    prefix = f"{today.year}.{today.month:02d}"
    existing = []
    for meta_path in glob.glob(os.path.join(PAPERS_DIR, "*", "meta.yaml")):
        with open(meta_path) as fh:
            m = yaml.safe_load(fh)
        if m["id"].startswith(prefix):
            existing.append(int(m["id"].rsplit(".", 1)[1]))
    return f"{prefix}.{(max(existing) + 1 if existing else 1):03d}"


def publish(session, project=None):
    meta = session.meta()
    if meta["status"] not in ("reviewed", "submitted"):
        raise RuntimeError(f"session status is {meta['status']!r}; review it first")
    project = project or session.project

    reviews = []
    for rdir in sorted(glob.glob(os.path.join(session.dir, "reviews", "round*"))):
        with open(os.path.join(rdir, "review.json")) as fh:
            reviews.append(json.load(fh))
    if not reviews:
        raise RuntimeError("no reviews found; refusing to publish unreviewed work")
    final = reviews[-1]

    paper_md = os.path.join(session.workspace, "paper", "paper.md")
    with open(paper_md, errors="replace") as fh:
        text = fh.read()
    fm = yaml.safe_load(harness.FRONT_RE.match(text).group(1))

    pid = next_paper_id()
    pdir = os.path.join(PAPERS_DIR, pid)
    os.makedirs(pdir)
    shutil.copytree(os.path.join(session.workspace, "paper"),
                    os.path.join(pdir, "paper"), symlinks=True)
    shutil.copy(session.transcript.path, os.path.join(pdir, "transcript.jsonl"))
    # raw.json is the reviewer CLI's internal wrapper output (includes host cwd
    # paths); publish only the structured review + letter.
    shutil.copytree(os.path.join(session.dir, "reviews"),
                    os.path.join(pdir, "reviews"),
                    ignore=shutil.ignore_patterns("raw.json"))

    pmeta = {
        "id": pid,
        "substrate_id": f"substrate:{pid}",
        "title": fm.get("title", "(untitled)"),
        "keywords": fm.get("keywords", []),
        "author_id": meta["author_id"],
        "author_display": meta["display"],
        "author_vendor": meta["vendor"],
        "session_id": meta["session_id"],
        "session_started": meta["started"],
        "submitted_at": meta.get("submitted_at"),
        "published": dt.date.today().isoformat(),
        "rounds": len(reviews),
        "reviewer": final.get("reviewer"),
        "reviewer_model_id": final.get("reviewer_model_id"),
        "decision_trail": [r["decision"] for r in reviews],
        "artifact_hash": meta.get("artifact_hash"),
        "turns_used": meta.get("turns_used"),
        "review_summary": final.get("summary", ""),
        "integrity_notes": final.get("integrity_notes", []),
        "citation_checks": final.get("citation_checks", []),
        "project_id": project.id if project else None,
        "session_index": meta.get("index"),
    }
    with open(os.path.join(pdir, "meta.yaml"), "w") as fh:
        yaml.safe_dump(pmeta, fh, sort_keys=False, allow_unicode=True)

    session.update_meta(status="published", paper_id=pid)
    session.transcript.append({"ev": "published", "paper_id": pid})
    if project:
        pm = project.meta()
        if pid not in pm["papers"]:
            pm["papers"].append(pid)
            project.update_meta(papers=pm["papers"])
    return pid
