"""Peer review by a frontier cloud model via the Claude Code CLI (headless).

The reviewer gets the paper, the code, and the full session transcript, and
returns a structured verdict. Round 1 may request one revision; round 2 always
ends in publication, with unresolved integrity notes published at the top of
the paper.
"""

import json
import os
import re
import subprocess

from . import harness

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))

EXEC_OUT_CAP = 2500       # chars of each exec output shown to the reviewer
BUNDLE_CAP = 350_000      # total char guard (Opus context is large but finite)
CODE_FILE_CAP = 12_000

CODE_EXT = {".py", ".sh", ".r", ".jl", ".js", ".sql", ".txt", ".csv", ".json", ".md", ".yaml", ".yml"}


def build_bundle(session):
    parts = []
    meta = session.meta()
    parts.append(f"# Submission bundle — session {meta['session_id']}\n"
                 f"Author model: {meta['author_id']} ({meta['display']}, {meta['vendor']})\n"
                 f"Round: {meta['round']}  Artifact hash: {meta.get('artifact_hash')}\n")

    paper_md = os.path.join(session.workspace, "paper", "paper.md")
    with open(paper_md, errors="replace") as fh:
        parts.append("## PAPER (paper/paper.md)\n\n```markdown\n" + fh.read() + "\n```\n")

    listing = []
    for dirpath, dirs, files in os.walk(session.workspace):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))]
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, session.workspace)
            if os.path.islink(p):
                listing.append(f"{rel}  (SYMLINK — excluded from bundle)")
                continue
            listing.append(f"{rel}  ({os.path.getsize(p)} bytes)")
    parts.append("## WORKSPACE FILE LISTING\n\n" + "\n".join(listing) + "\n")

    code_parts = []
    for dirpath, dirs, files in os.walk(session.workspace):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, session.workspace)
            ext = os.path.splitext(f)[1].lower()
            if rel in ("paper/paper.md", "SUBSTRATE.md") or ext not in CODE_EXT:
                continue
            if os.path.islink(p):
                continue
            with open(p, errors="replace") as fh:
                content = fh.read(CODE_FILE_CAP + 1)
            trunc = "\n…[truncated]" if len(content) > CODE_FILE_CAP else ""
            code_parts.append(f"### {rel}\n```\n{content[:CODE_FILE_CAP]}{trunc}\n```")
    if code_parts:
        parts.append("## WORKSPACE FILES\n\n" + "\n\n".join(code_parts) + "\n")

    tparts = ["## SESSION TRANSCRIPT (append-only, recorded by the harness — "
              "the author could not edit this)\n"]
    for ev in session.transcript.events():
        if ev["ev"] == "assistant":
            tparts.append(f"--- AUTHOR (turn) ---\n{ev['text']}")
        elif ev["ev"] == "exec":
            out = _cap(ev.get("stdout", ""), EXEC_OUT_CAP)
            err = _cap(ev.get("stderr", ""), EXEC_OUT_CAP // 2)
            tparts.append(
                f"--- EXEC (exit {ev['exit']}, {ev.get('duration_s', '?')}s) ---\n"
                f"$ {ev['cmd']}\n{out}" + (f"\n[stderr]\n{err}" if err else ""))
        elif ev["ev"] == "notice":
            tparts.append(f"--- HARNESS ---\n{ev['text']}")
    parts.append("\n\n".join(tparts))

    bundle = "\n\n".join(parts)
    if len(bundle) > BUNDLE_CAP:
        bundle = (bundle[:BUNDLE_CAP]
                  + "\n\n[bundle truncated at cap — flag this in your review if "
                  "it prevented verification]")
    return bundle


def _cap(text, n):
    if not text:
        return ""
    return text if len(text) <= n else text[:n] + f"\n…[+{len(text)-n} chars]"


def run_review(session, reviewer_key="opus", timeout=2400):
    reg = harness.load_registry()["reviewers"][reviewer_key]
    meta = session.meta()
    round_no = meta["round"]
    final_round = round_no >= 2

    with open(os.path.join(ENGINE_DIR, "prompts", "reviewer.md")) as fh:
        prompt = fh.read()
    prompt = (prompt
              .replace("[[REVIEWER_DISPLAY]]", reg["display"])
              .replace("[[ROUND]]", str(round_no))
              .replace("[[FINAL_ROUND]]", "yes" if final_round else "no"))

    bundle = build_bundle(session)
    full_prompt = prompt + "\n\n---\n\n" + bundle

    # WebSearch only — not WebFetch. The prompt contains author-controlled text;
    # WebFetch would let a crafted "citation URL" exfiltrate bundle contents to an
    # arbitrary server. Search results are enough to verify citations.
    proc = subprocess.run(
        ["claude", "-p", "--model", reg["model_id"],
         "--allowedTools", "WebSearch",
         "--output-format", "json"],
        input=full_prompt, capture_output=True, text=True,
        timeout=timeout, cwd=session.dir,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed ({proc.returncode}): {proc.stderr[:2000]}")

    wrapper = json.loads(proc.stdout)
    result_text = wrapper.get("result", "")
    review = parse_review_json(result_text)
    review["reviewer"] = reg["display"]
    review["reviewer_model_id"] = reg["model_id"]
    review["round"] = round_no
    if final_round:
        review["decision"] = "publish"

    rdir = os.path.join(session.dir, "reviews", f"round{round_no}")
    os.makedirs(rdir, exist_ok=True)
    with open(os.path.join(rdir, "review.json"), "w") as fh:
        json.dump(review, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(rdir, "letter.md"), "w") as fh:
        fh.write(review.get("letter", ""))
    with open(os.path.join(rdir, "raw.json"), "w") as fh:
        fh.write(proc.stdout)

    session.transcript.append({
        "ev": "review", "round": round_no, "reviewer": reg["display"],
        "decision": review["decision"],
        "n_notes": len(review.get("integrity_notes", [])),
    })
    session.update_meta(status="reviewed", last_decision=review["decision"])
    return review


def parse_review_json(text):
    m = None
    for m in re.finditer(r"```json\s*\n(.*?)\n```", text, re.DOTALL):
        pass  # keep the last fenced json block
    candidate = m.group(1) if m else text
    try:
        review = json.loads(candidate)
    except json.JSONDecodeError:
        brace = candidate.find("{")
        review = json.loads(candidate[brace:candidate.rfind("}") + 1])
    if review.get("decision") not in ("publish", "revise"):
        raise ValueError(f"review decision missing/invalid: {review.get('decision')!r}")
    review.setdefault("integrity_notes", [])
    review.setdefault("summary", "")
    review.setdefault("letter", "")
    return review
