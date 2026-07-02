"""Substrate research session harness.

Drives a local model through a research session:

    model message --> harness extracts one ```run block --> executes in sandbox
                  --> feedback (exit code + output) appended --> model continues
                  --> ... --> model emits ```submit --> validation --> frozen

Everything is recorded in an append-only transcript.jsonl that lives OUTSIDE
the sandbox. The transcript is the provenance record reviewers audit against;
the author cannot modify it.
"""

import hashlib
import json
import os
import re
import time
import datetime as dt

import requests
import yaml

from . import sandbox

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ENGINE_DIR)
WORK_DIR = os.path.join(ROOT, "work")      # legacy one-shot sessions
LABS_DIR = os.path.join(ROOT, "labs")      # persistent longitudinal projects
QUEUE_DIR = os.path.join(ROOT, "queue")
PREEMPT_FLAG = os.path.join(QUEUE_DIR, "preempt")

NOTEBOOK = "NOTEBOOK.md"   # curated by the model — its durable working memory
LOG = "LOG.md"             # append-only by the harness — one entry per session

STDOUT_CAP = 8000       # chars kept per exec at record time
STDERR_CAP = 4000
RECENT_FULL = 6         # last N exec outputs sent to the model in full
ELIDED_HEAD = 300       # chars of older outputs kept in the model's context
CONTEXT_CHAR_BUDGET = 100_000  # rough guard for 32k-token local models

RUN_RE = re.compile(r"```run[ \t]*\n(.*)\n```", re.DOTALL)
SUBMIT_RE = re.compile(r"```submit[ \t]*\n?.*?```", re.DOTALL)

REQUIRED_SECTIONS = ["abstract", "introduction", "method", "results",
                     "limitations", "references"]


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_registry():
    with open(os.path.join(ENGINE_DIR, "models.yaml")) as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------- transcript

class Transcript:
    def __init__(self, path):
        self.path = path

    def append(self, ev: dict):
        ev = {"t": now_iso(), **ev}
        with open(self.path, "a") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def events(self):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


# ------------------------------------------------------------- meta mixin

class _MetaStore:
    meta_path = None  # set by subclass

    def meta(self):
        with open(self.meta_path) as fh:
            return json.load(fh)

    def update_meta(self, **kv):
        m = self.meta()
        m.update(kv)
        with open(self.meta_path, "w") as fh:
            json.dump(m, fh, indent=2)
        return m


def _new_session_id(model_key):
    return "{}-{}-{}".format(
        dt.datetime.now().strftime("%Y%m%d-%H%M"),
        model_key,
        hashlib.sha1(os.urandom(8)).hexdigest()[:4],
    )


def _resolve_session(session_id):
    """Locate a session by id in either the legacy work/ tree or a project."""
    legacy = os.path.join(WORK_DIR, session_id)
    if os.path.isdir(legacy):
        return legacy, os.path.join(legacy, "workspace"), None
    for pid in os.listdir(LABS_DIR) if os.path.isdir(LABS_DIR) else []:
        cand = os.path.join(LABS_DIR, pid, "sessions", session_id)
        if os.path.isdir(cand):
            return cand, os.path.join(LABS_DIR, pid, "workspace"), pid
    raise FileNotFoundError(f"no session {session_id!r} in work/ or labs/")


# ------------------------------------------------------------------ session

class Session(_MetaStore):
    def __init__(self, session_id, project=None, _dir=None, _workspace=None):
        self.id = session_id
        self.project = project
        if _dir is not None:
            self.dir, self.workspace = _dir, _workspace
        elif project is not None:
            self.dir = os.path.join(project.dir, "sessions", session_id)
            self.workspace = project.workspace
        else:
            self.dir, self.workspace, pid = _resolve_session(session_id)
            if pid:
                self.project = Project(pid)
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.transcript = Transcript(os.path.join(self.dir, "transcript.jsonl"))

    @classmethod
    def create(cls, model_key, max_turns=60, max_minutes=150, exec_timeout=600):
        """A standalone one-shot session (legacy path, no persistent project)."""
        reg = load_registry()["models"][model_key]
        sid = _new_session_id(model_key)
        s = cls(sid, _dir=os.path.join(WORK_DIR, sid),
                _workspace=os.path.join(WORK_DIR, sid, "workspace"))
        os.makedirs(s.workspace)
        meta = _base_session_meta(reg, model_key, sid, max_turns, max_minutes, exec_timeout)
        with open(s.meta_path, "w") as fh:
            json.dump(meta, fh, indent=2)
        s.transcript.append({"ev": "meta", "meta": meta})
        write_workspace_readme(s, reg, max_turns, max_minutes, exec_timeout, longitudinal=False)
        return s


def _base_session_meta(reg, model_key, sid, max_turns, max_minutes, exec_timeout):
    return {
        "session_id": sid,
        "model_key": model_key,
        "author_id": reg["author_id"],
        "display": reg["display"],
        "vendor": reg.get("vendor", ""),
        "started": now_iso(),
        "date": dt.date.today().isoformat(),
        "status": "created",
        "round": 0,
        "turns_used": 0,
        "max_turns": max_turns,
        "max_minutes": max_minutes,
        "exec_timeout": exec_timeout,
    }


# ------------------------------------------------------------------ project

class Project(_MetaStore):
    """A persistent, longitudinal research project owned by one model.

    The workspace lives on across sessions — it *is* the memory. The model
    curates NOTEBOOK.md; the harness keeps an append-only LOG.md. A project
    runs one bounded session at a time (typically once a day) and may go many
    sessions before — or without ever — producing a publishable paper.
    """

    def __init__(self, project_id):
        self.id = project_id
        self.dir = os.path.join(LABS_DIR, project_id)
        self.workspace = os.path.join(self.dir, "workspace")
        self.meta_path = os.path.join(self.dir, "meta.json")

    @classmethod
    def create(cls, model_key, title=None):
        reg = load_registry()["models"][model_key]
        pid = "{}-{}".format(model_key, hashlib.sha1(os.urandom(8)).hexdigest()[:6])
        p = cls(pid)
        os.makedirs(p.workspace)
        os.makedirs(os.path.join(p.dir, "sessions"))
        meta = {
            "project_id": pid,
            "model_key": model_key,
            "author_id": reg["author_id"],
            "display": reg["display"],
            "vendor": reg.get("vendor", ""),
            "created": now_iso(),
            "status": "active",
            "title": title,
            "sessions": [],
            "papers": [],
        }
        with open(p.meta_path, "w") as fh:
            json.dump(meta, fh, indent=2)
        _seed_memory(p.workspace, reg)
        return p

    def sessions(self):
        return [Session(s["session_id"], project=self) for s in self.meta()["sessions"]]

    def start_session(self, max_turns=40, max_minutes=90, exec_timeout=600):
        reg = load_registry()["models"][self.meta()["model_key"]]
        pmeta = self.meta()
        sid = _new_session_id(pmeta["model_key"])
        s = Session(sid, project=self)
        os.makedirs(s.dir)
        meta = _base_session_meta(reg, pmeta["model_key"], sid,
                                  max_turns, max_minutes, exec_timeout)
        meta["project_id"] = self.id
        meta["index"] = len(pmeta["sessions"]) + 1
        with open(s.meta_path, "w") as fh:
            json.dump(meta, fh, indent=2)
        s.transcript.append({"ev": "meta", "meta": meta})
        pmeta["sessions"].append({"session_id": sid, "index": meta["index"],
                                  "date": meta["date"], "status": "created"})
        self.update_meta(sessions=pmeta["sessions"])
        _seed_memory(self.workspace, reg)  # ensure NOTEBOOK/LOG exist
        write_workspace_readme(s, reg, max_turns, max_minutes, exec_timeout,
                               longitudinal=True)
        return s

    def record_session_outcome(self, session, outcome):
        pmeta = self.meta()
        for entry in pmeta["sessions"]:
            if entry["session_id"] == session.id:
                entry["status"] = outcome
                entry["turns_used"] = session.meta().get("turns_used", 0)
        self.update_meta(sessions=pmeta["sessions"])


def _seed_memory(workspace, reg):
    nb = os.path.join(workspace, NOTEBOOK)
    if not os.path.exists(nb):
        with open(nb, "w") as fh:
            fh.write(NOTEBOOK_TEMPLATE.format(author=reg["author_id"]))
    lg = os.path.join(workspace, LOG)
    if not os.path.exists(lg):
        with open(lg, "w") as fh:
            fh.write("# Session log\n\nAppend-only, written by the harness. "
                     "One entry per session.\n")


NOTEBOOK_TEMPLATE = """# Lab notebook — {author}

This is your durable memory. Each session starts with a fresh context window,
so this file is how you remember what you are doing and why. Keep it current:
it is the first thing you will read next session.

## Research direction
_What am I investigating, and why? (You choose this. It can be anything.)_

(not yet chosen)

## Status
_Where things stand right now._

Nothing done yet — this is session 1.

## Findings so far
_Concrete results, each with how it was produced (script, command). Numbers only
if a command actually produced them._

## Next steps
_The very next things to do. Write these for your future self._

- Decide on a research direction I can actually pursue with the tools here.
"""


def write_workspace_readme(session, reg, max_turns, max_minutes, exec_timeout,
                           longitudinal):
    txt = build_system_prompt(reg, max_turns, max_minutes, exec_timeout, longitudinal)
    with open(os.path.join(session.workspace, "SUBSTRATE.md"), "w") as fh:
        fh.write("<!-- Session instructions. Also your system prompt. -->\n\n" + txt)


# ------------------------------------------------------------------- prompt

def build_system_prompt(reg, max_turns, max_minutes, exec_timeout, longitudinal=True):
    name = "researcher.md" if longitudinal else "researcher-oneshot.md"
    path = os.path.join(ENGINE_DIR, "prompts", name)
    if not os.path.exists(path):
        path = os.path.join(ENGINE_DIR, "prompts", "researcher.md")
    with open(path) as fh:
        txt = fh.read()
    subs = {
        "AUTHOR_ID": reg["author_id"],
        "MODEL_DISPLAY": reg["display"],
        "MAX_TURNS": str(max_turns),
        "MAX_MINUTES": str(max_minutes),
        "EXEC_TIMEOUT": str(exec_timeout),
        "SELF_API": reg.get("self_api", "No local inference API is available."),
    }
    for k, v in subs.items():
        txt = txt.replace("[[" + k + "]]", v)
    return txt


NOTEBOOK_SOFT_CAP = 9000   # chars; beyond this the model is nudged to consolidate
LOG_WINDOW = 4             # most-recent session entries injected at session start


def _recent_log_entries(log_text, n):
    """Return the last n `## Session` entries; older history stays on disk.

    A sliding window over immutable journal entries — bounded context without
    truncating mid-entry (the failure mode of a blind character tail)."""
    parts = re.split(r"(?=^## Session )", log_text, flags=re.MULTILINE)
    entries = [p for p in parts if p.strip().startswith("## Session")]
    if not entries:
        return log_text.strip()
    shown = entries[-n:]
    prefix = ""
    if len(entries) > n:
        prefix = (f"…[{len(entries) - n} earlier session entries omitted here; "
                  f"the full history is in {LOG} — `cat {LOG}` to read it]\n\n")
    return prefix + "".join(shown).strip()


def session_opening_notice(project, session):
    """The memory handed to the model at the start of a project session:
    which session this is, its curated notebook, and the recent session log."""
    meta = session.meta()
    idx = meta.get("index", 1)
    nb_path = os.path.join(project.workspace, NOTEBOOK)
    log_path = os.path.join(project.workspace, LOG)
    notebook = open(nb_path).read() if os.path.exists(nb_path) else "(none)"
    log = _recent_log_entries(open(log_path).read(), LOG_WINDOW) \
        if os.path.exists(log_path) else "(none)"
    nb_nudge = ""
    if len(notebook) > NOTEBOOK_SOFT_CAP:
        nb_nudge = ("\n[harness] Your notebook is getting long — during this session, "
                    "consolidate it: keep what's load-bearing (direction, current "
                    "status, key findings with provenance, next steps) and cut the rest.")
    if idx == 1:
        head = ("[harness] This is session 1 of a new, ongoing research project that "
                "is yours. You choose what to investigate — anything you can pursue "
                "with the tools here. Work you don't finish today continues in future "
                "sessions; your workspace persists between them.")
    else:
        head = (f"[harness] This is session {idx} of your ongoing project. Your "
                "workspace is exactly as you left it last session. Read your notebook "
                "below, then continue where you left off (start by checking your files "
                "with `ls -R` and re-reading anything you need).")
    return (f"{head}\n\nToday is {meta['date']}.{nb_nudge}\n\n"
            f"===== YOUR LAB NOTEBOOK ({NOTEBOOK}) =====\n{notebook}\n"
            f"===== END NOTEBOOK =====\n\n"
            f"===== SESSION LOG ({LOG}, recent) =====\n{log}\n===== END LOG =====")


def wrap_session(session, project, reg, outcome):
    """After a project session ends: get a short summary from the model, append
    it to LOG.md, and nudge the notebook. Best-effort; never fatal."""
    meta = session.meta()
    idx = meta.get("index", 1)
    try:
        msgs = build_messages(session, reg)
        msgs.append({"role": "user", "content":
            "[harness] This session is ending. In 2–4 sentences, plainly summarize "
            "what you actually did this session and what your next session should do. "
            "No fenced blocks — just the summary."})
        summary = chat(reg, msgs).strip()
    except Exception as err:  # noqa: BLE001 - continuity must not crash on this
        summary = f"(summary unavailable: {err})"
    entry = (f"\n## Session {idx} — {meta['date']} "
             f"({meta.get('turns_used', 0)} turns, {outcome})\n\n{summary}\n")
    with open(os.path.join(project.workspace, LOG), "a") as fh:
        fh.write(entry)
    session.transcript.append({"ev": "handoff", "text": summary})
    session.update_meta(handoff=summary)


# ------------------------------------------------------------------ chat api

def chat(reg, messages):
    """One chat completion against a local model. Returns assistant text."""
    last_err = None
    for attempt in range(3):
        try:
            if reg["api"] == "ollama":
                resp = requests.post(
                    reg["endpoint"] + "/api/chat",
                    json={
                        "model": reg["api_model"],
                        "messages": messages,
                        "stream": False,
                        "options": {
                            "num_ctx": reg.get("num_ctx", 32768),
                            "temperature": reg.get("temperature", 0.8),
                        },
                    },
                    timeout=900,
                )
                resp.raise_for_status()
                return resp.json()["message"]["content"]
            else:  # openai-compatible
                resp = requests.post(
                    reg["endpoint"] + "/v1/chat/completions",
                    json={
                        "model": reg["api_model"],
                        "messages": messages,
                        "temperature": reg.get("temperature", 0.8),
                        "max_tokens": 4096,
                    },
                    timeout=900,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as err:
            last_err = err
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"model API failed after retries: {last_err}")


# ------------------------------------------------- context from transcript

def build_messages(session, reg):
    """Rebuild the chat context from the transcript, eliding old output."""
    meta = session.meta()
    system = build_system_prompt(
        reg, meta["max_turns"], meta["max_minutes"], meta["exec_timeout"],
        longitudinal=session.project is not None)
    msgs = [{"role": "system", "content": system}]

    turns = []  # list of {"assistant": str, "feedback": str, "is_exec": bool}
    for ev in session.transcript.events():
        if ev["ev"] == "assistant":
            turns.append({"assistant": ev["text"], "feedback": None, "is_exec": False})
        elif ev["ev"] == "exec" and turns:
            turns[-1]["feedback"] = format_exec_feedback(ev)
            turns[-1]["is_exec"] = True
        elif ev["ev"] == "notice" and turns and turns[-1]["feedback"] is None:
            turns[-1]["feedback"] = ev["text"]
        elif ev["ev"] == "notice" and (not turns or turns[-1]["feedback"] is not None):
            # standalone notice (e.g. review letter on resume) -> user message
            turns.append({"assistant": None, "feedback": ev["text"], "is_exec": False})

    # elide exec outputs older than the last RECENT_FULL
    exec_idx = [i for i, t in enumerate(turns) if t["is_exec"]]
    for n, i in enumerate(exec_idx):
        if n < len(exec_idx) - RECENT_FULL:
            fb = turns[i]["feedback"]
            if len(fb) > ELIDED_HEAD + 100:
                turns[i]["feedback"] = (
                    fb[:ELIDED_HEAD]
                    + "\n…[older output elided to save context — re-run the "
                    "command or cat a file if you need it again]"
                )

    # hard char budget: drop oldest whole turns behind a stub
    def total(ts):
        return sum(len(t["assistant"] or "") + len(t["feedback"] or "") for t in ts)
    dropped = 0
    while len(turns) > 4 and total(turns) > CONTEXT_CHAR_BUDGET:
        turns.pop(0)
        dropped += 1
    if dropped:
        msgs.append({"role": "user", "content":
                     f"[harness] {dropped} earliest turns of this session were "
                     "dropped from your context for length. Your files in the "
                     "workspace are unaffected; SUBSTRATE.md has your instructions."})

    for t in turns:
        if t["assistant"] is not None:
            msgs.append({"role": "assistant", "content": t["assistant"]})
        if t["feedback"] is not None:
            msgs.append({"role": "user", "content": t["feedback"]})
    return msgs


def format_exec_feedback(ev):
    parts = [f"[harness] exit code {ev['exit']}"
             + (" (TIMED OUT)" if ev.get("timed_out") else "")]
    if ev.get("stdout"):
        parts.append("stdout:\n" + ev["stdout"])
    if ev.get("stderr"):
        parts.append("stderr:\n" + ev["stderr"])
    if not ev.get("stdout") and not ev.get("stderr"):
        parts.append("(no output)")
    return "\n".join(parts)


def cap(text, n):
    if text is None:
        return ""
    if len(text) <= n:
        return text
    return text[:n] + f"\n…[truncated by harness: {len(text) - n} chars omitted]"


# ---------------------------------------------------------------- main loop

def run_session(session, opening_notice=None, on_event=None):
    """Run (or continue) the session loop until submit / budget / preemption.

    Longitudinal (project) sessions are not expected to submit a paper — most
    just advance the work and end 'advanced'. One-shot sessions still nudge
    toward submission on budget exhaustion.
    """
    meta = session.meta()
    reg = load_registry()["models"][meta["model_key"]]
    longitudinal = session.project is not None
    session.update_meta(status="running")

    if opening_notice:
        session.transcript.append({"ev": "notice", "text": opening_notice})

    started = time.time()
    grace_turns = 0
    preempting = False

    while True:
        meta = session.meta()
        turns_left = meta["max_turns"] - meta["turns_used"]
        mins_left = meta["max_minutes"] - (time.time() - started) / 60

        # preemption: the GPU is needed elsewhere. Give one final turn to
        # checkpoint the notebook, then stop. The project resumes next session.
        if not preempting and os.path.exists(PREEMPT_FLAG):
            preempting = True
            session.transcript.append({"ev": "notice", "text":
                "[harness] PREEMPTION — the GPU is needed elsewhere and this session "
                "is ending now. This is your final turn: update NOTEBOOK.md with your "
                "current state and next steps in one ```run block, then stop. Your "
                "workspace persists; you continue next session."})

        if not preempting and (turns_left <= 0 or mins_left <= 0):
            if grace_turns >= 2:
                end = "advanced" if longitudinal else "incomplete"
                session.update_meta(status=end)
                session.transcript.append({"ev": "notice", "text":
                    "[harness] session ended: budget exhausted."})
                return end
            grace_turns += 1
            if longitudinal:
                session.transcript.append({"ev": "notice", "text":
                    "[harness] BUDGET NEARLY SPENT. Wrap up: make sure NOTEBOOK.md "
                    "captures your progress and next steps for the next session. "
                    "If (and only if) you have a finding genuinely worth publishing, "
                    "you may write paper/paper.md and ```submit```; otherwise just "
                    "update the notebook — an ordinary session ends without a paper."})
            else:
                session.transcript.append({"ev": "notice", "text":
                    "[harness] BUDGET EXHAUSTED. If paper/paper.md is ready, reply with "
                    "a ```submit``` block now to submit it as-is. Otherwise this "
                    "session ends incomplete."})

        msgs = build_messages(session, reg)
        text = chat(reg, msgs)
        session.transcript.append({"ev": "assistant", "text": text})
        session.update_meta(turns_used=meta["turns_used"] + 1)
        if on_event:
            on_event("assistant", text)

        if SUBMIT_RE.search(text):
            problems = validate_submission(session)
            if problems:
                session.transcript.append({"ev": "notice", "text":
                    "[harness] submission rejected, fix and submit again:\n- "
                    + "\n- ".join(problems)})
                if on_event:
                    on_event("reject", problems)
                continue
            finalize_submission(session)
            if on_event:
                on_event("submitted", None)
            return "submitted"

        m = RUN_RE.search(text)
        if not m:
            if preempting:
                session.update_meta(status="preempted")
                return "preempted"
            session.transcript.append({"ev": "notice", "text":
                "[harness] no ```run block found and no ```submit block. "
                "Emit exactly one fenced block tagged `run` containing bash, "
                "or a ```submit``` block when your paper is ready."})
            continue

        cmd = m.group(1)
        extra = len(RUN_RE.findall(text)) > 1
        t0 = time.time()
        code, out, err, timed_out = sandbox.run(
            session.workspace, cmd, timeout=meta["exec_timeout"])
        ev = {
            "ev": "exec", "cmd": cmd, "exit": code,
            "stdout": cap(out, STDOUT_CAP), "stderr": cap(err, STDERR_CAP),
            "timed_out": timed_out, "duration_s": round(time.time() - t0, 1),
        }
        session.transcript.append(ev)
        if on_event:
            on_event("exec", ev)
        if extra:
            session.transcript.append({"ev": "notice", "text":
                "[harness] note: multiple ```run blocks found; only the first was executed."})

        if preempting:  # the checkpoint turn just ran; stop now
            session.update_meta(status="preempted")
            return "preempted"

        # periodic budget reminders
        meta = session.meta()
        turns_left = meta["max_turns"] - meta["turns_used"]
        if turns_left in (10, 5) and not longitudinal:
            session.transcript.append({"ev": "notice", "text":
                f"[harness] {turns_left} command turns remaining. Budget time for "
                "writing paper/paper.md and submitting."})


# ------------------------------------------------------------- submission

FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def validate_submission(session):
    problems = []
    paper_dir = os.path.join(session.workspace, "paper")
    paper_md = os.path.join(paper_dir, "paper.md")
    if not os.path.exists(paper_md):
        return ["paper/paper.md does not exist"]

    # symlinks are rejected outright: the pipeline reads the workspace from
    # outside the sandbox, so a link could smuggle host files into the
    # published record
    for dirpath, dirs, files in os.walk(session.workspace):
        for name in dirs + files:
            if os.path.islink(os.path.join(dirpath, name)):
                rel = os.path.relpath(os.path.join(dirpath, name), session.workspace)
                problems.append(f"symlink not allowed in workspace: {rel} (copy the file instead)")
    if problems:
        return problems
    with open(paper_md, errors="replace") as fh:
        text = fh.read()
    if len(text.strip()) < 500:
        problems.append("paper/paper.md is implausibly short (<500 chars)")

    m = FRONT_RE.match(text)
    if not m:
        problems.append("paper.md must start with YAML front matter (---) containing at least a title")
    else:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
            if not fm.get("title"):
                problems.append("front matter is missing `title`")
        except yaml.YAMLError as err:
            problems.append(f"front matter is not valid YAML: {err}")

    lowered = text.lower()
    for sec in REQUIRED_SECTIONS:
        if not re.search(r"^#{1,3}\s+.*" + sec, lowered, re.MULTILINE):
            problems.append(f"missing required section heading: {sec.title()}")

    paper_root = os.path.realpath(paper_dir)
    for link in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        if link.startswith(("http://", "https://", "data:")):
            problems.append(f"figure must be a local file in paper/, not a URL: {link}")
            continue
        target = os.path.realpath(os.path.join(paper_dir, link.split("#")[0]))
        if not (target == paper_root or target.startswith(paper_root + os.sep)):
            problems.append(f"figure path escapes paper/: {link}")
            continue
        if not os.path.exists(target):
            problems.append(f"figure referenced but file not found: {link}")
    return problems


def finalize_submission(session):
    """Stamp front matter, hash artifacts, freeze status."""
    meta = session.meta()
    paper_dir = os.path.join(session.workspace, "paper")
    paper_md = os.path.join(paper_dir, "paper.md")
    with open(paper_md, errors="replace") as fh:
        text = fh.read()

    m = FRONT_RE.match(text)
    fm = yaml.safe_load(m.group(1)) or {}
    body = text[m.end():]
    fm["author"] = meta["author_id"]           # harness-stamped, not author-chosen
    fm["session"] = meta["session_id"]
    fm["date"] = dt.date.today().isoformat()
    fm["round"] = meta["round"]
    new_text = "---\n" + yaml.safe_dump(fm, sort_keys=False,
                                        allow_unicode=True).strip() + "\n---\n" + body
    with open(paper_md, "w") as fh:
        fh.write(new_text)

    digest = hash_tree(paper_dir)
    session.update_meta(status="submitted", submitted_at=now_iso(),
                        artifact_hash=digest, round=meta["round"] + 1)
    session.transcript.append({"ev": "submit", "artifact_hash": digest,
                               "round": meta["round"] + 1})


def hash_tree(root):
    entries = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            entries.append(f"{rel}:{h.hexdigest()}")
    return "sha256:" + hashlib.sha256("\n".join(entries).encode()).hexdigest()
