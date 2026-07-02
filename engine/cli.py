"""substrate — the whole journal from one command.

Longitudinal (the primary mode):
  substrate project new <model>        start a persistent research project
  substrate project run <project-id>   run one session (resumes from its notebook)
  substrate project list               list projects
  substrate daily [--model M]          one scheduled tick: pick an active project,
                                       run a session, review+publish if it submitted,
                                       deploy. Skips if the GPU is busy.
  substrate stop                       ask the running session to checkpoint and yield
                                       the GPU (drops queue/preempt)

Per-session / one-shot:
  substrate new <model>                a standalone one-shot session
  substrate run <session-id>           run/continue a session
  substrate review <session-id>        run a review round (claude -p)
  substrate revise <session-id>        resume with the latest review letter
  substrate publish <session-id>       move an accepted paper to papers/
  substrate auto <model>               one-shot: new→run→review→(revise)→publish→build

Site:
  substrate build | deploy | status
"""

import argparse
import fcntl
import glob
import json
import os
import subprocess
import sys

import yaml

from . import harness, publish as publish_mod, reviewer

ROOT = harness.ROOT
QUEUE_DIR = harness.QUEUE_DIR
GPU_BUSY_MB = 2000  # if more than this is already in use, assume the GPU is taken


def log(msg):
    print(f"[substrate] {msg}", flush=True)


def gpu_lock(block=True):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    fh = open(os.path.join(QUEUE_DIR, "gpu.lock"), "w")
    flags = fcntl.LOCK_EX if block else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    if block:
        log("waiting for GPU lease (queue/gpu.lock)…")
    try:
        fcntl.flock(fh, flags)
    except BlockingIOError:
        return None
    log("GPU lease acquired")
    return fh  # keep referenced; lock released when process exits


def gpu_used_mb():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True, timeout=15)
        return max(int(x) for x in out.split())
    except Exception:  # noqa: BLE001
        return 0


def echo_event(kind, payload):
    if kind == "assistant":
        head = payload.strip().replace("\n", " ")[:160]
        log(f"author: {head}")
    elif kind == "exec":
        log(f"  $ {payload['cmd'].splitlines()[0][:120]}  -> exit {payload['exit']} ({payload['duration_s']}s)")
    elif kind == "reject":
        log(f"submission rejected: {payload}")
    elif kind == "submitted":
        log("submission accepted and frozen")


def preflight(model_key):
    reg = harness.load_registry()["models"][model_key]
    cmd = reg.get("preflight")
    if cmd:
        log(f"preflight: {cmd}")
        subprocess.run(cmd, shell=True, check=True)


def clear_preempt():
    try:
        os.remove(harness.PREEMPT_FLAG)
    except FileNotFoundError:
        pass


# ----------------------------------------------------------------- projects

def cmd_project_new(args):
    p = harness.Project.create(args.model, title=args.title)
    log(f"created project {p.id} for {p.meta()['author_id']}")
    print(p.id)
    return p


def cmd_project_run(args):
    p = harness.Project(args.project)
    clear_preempt()
    preflight(p.meta()["model_key"])
    _lock = gpu_lock()
    s = p.start_session(max_turns=args.turns, max_minutes=args.minutes)
    log(f"session {s.meta()['index']} of project {p.id} → {s.id}")
    notice = harness.session_opening_notice(p, s)
    result = harness.run_session(s, opening_notice=notice, on_event=echo_event)
    reg = harness.load_registry()["models"][p.meta()["model_key"]]
    harness.wrap_session(s, p, reg, result)
    p.record_session_outcome(s, result)
    log(f"session ended: {result}")
    return s, result


def cmd_project_list(args):
    print("== projects ==")
    for mp in sorted(glob.glob(os.path.join(harness.LABS_DIR, "*", "meta.json"))):
        with open(mp) as fh:
            m = json.load(fh)
        title = m.get("title") or "(direction set by the model)"
        print(f"  {m['project_id']:<22} {m['status']:<8} "
              f"sessions={len(m['sessions'])} papers={len(m['papers'])}  "
              f"[{m['author_id']}] {title[:50]}")


def active_projects(model_key=None):
    out = []
    for mp in sorted(glob.glob(os.path.join(harness.LABS_DIR, "*", "meta.json"))):
        with open(mp) as fh:
            m = json.load(fh)
        if m["status"] == "active" and (model_key is None or m["model_key"] == model_key):
            out.append(m)
    return out


def cmd_daily(args):
    """One scheduled tick. Meant to run unattended, once a day."""
    if gpu_used_mb() > GPU_BUSY_MB:
        log(f"GPU busy ({gpu_used_mb()} MiB used > {GPU_BUSY_MB}); skipping today.")
        return 0
    lock = gpu_lock(block=False)
    if lock is None:
        log("another substrate run holds the GPU lease; skipping.")
        return 0

    model = args.model
    projects = active_projects(model)
    if not projects:
        log(f"no active project for {model}; creating one.")
        p = harness.Project.create(model)
    else:  # least-recently-run active project
        projects.sort(key=lambda m: (m["sessions"][-1]["date"] if m["sessions"] else "",
                                     len(m["sessions"])))
        p = harness.Project(projects[0]["project_id"])

    clear_preempt()
    preflight(p.meta()["model_key"])
    s = p.start_session(max_turns=args.turns, max_minutes=args.minutes)
    log(f"daily: project {p.id}, session {s.meta()['index']} → {s.id}")
    notice = harness.session_opening_notice(p, s)
    result = harness.run_session(s, opening_notice=notice, on_event=echo_event)
    reg = harness.load_registry()["models"][p.meta()["model_key"]]
    harness.wrap_session(s, p, reg, result)
    p.record_session_outcome(s, result)
    log(f"daily session ended: {result}")

    if result == "submitted":
        _review_publish_cycle(s, p, args)
    cmd_deploy(args)
    log("daily tick complete")
    return 0


def _review_publish_cycle(s, project, args):
    review = reviewer.run_review(s)
    log(f"round {review['round']} decision: {review['decision']} "
        f"({len(review['integrity_notes'])} integrity notes)")
    if review["decision"] == "revise":
        _revise(s, extra_turns=getattr(args, "extra_turns", 20))
        if s.meta()["status"] == "submitted":
            reviewer.run_review(s)
    pid = publish_mod.publish(s, project=project)
    log(f"published as substrate:{pid}")


def cmd_stop(args):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    with open(harness.PREEMPT_FLAG, "w") as fh:
        fh.write("stop requested\n")
    log("preempt flag set — the running session will checkpoint and yield the GPU.")


# ------------------------------------------------------- one-shot / shared

def cmd_new(args):
    s = harness.Session.create(args.model, max_turns=args.turns,
                               max_minutes=args.minutes)
    log(f"created session {s.id}")
    print(s.id)
    return s


def cmd_run(args):
    s = harness.Session(args.session)
    clear_preempt()
    preflight(s.meta()["model_key"])
    _lock = gpu_lock()
    result = harness.run_session(s, on_event=echo_event)
    log(f"session ended: {result}")
    return result


def cmd_review(args):
    s = harness.Session(args.session)
    log("sending to reviewer (claude -p, this can take several minutes)…")
    review = reviewer.run_review(s)
    log(f"round {review['round']} decision: {review['decision']} "
        f"({len(review['integrity_notes'])} integrity notes)")
    return review


def _revise(s, extra_turns):
    rounds = sorted(glob.glob(os.path.join(s.dir, "reviews", "round*")))
    with open(os.path.join(rounds[-1], "letter.md")) as fh:
        letter = fh.read()
    notice = ("[harness] REVIEW RECEIVED — decision: revise. The review letter "
              "follows. Address each numbered point (running commands as needed), "
              "update paper/paper.md, then submit again.\n\n" + letter)
    preflight(s.meta()["model_key"])
    _lock = gpu_lock()
    m = s.meta()
    s.update_meta(max_turns=m["turns_used"] + extra_turns, status="revising")
    return harness.run_session(s, opening_notice=notice, on_event=echo_event)


def cmd_revise(args):
    result = _revise(harness.Session(args.session), args.extra_turns)
    log(f"revision ended: {result}")
    return result


def cmd_publish(args):
    s = harness.Session(args.session)
    pid = publish_mod.publish(s, project=s.project)
    log(f"published as substrate:{pid} -> papers/{pid}/")
    return pid


def cmd_auto(args):
    s = cmd_new(args)
    args.session = s.id
    result = cmd_run(args)
    if result != "submitted":
        log("no submission; stopping (session can be inspected/resumed)")
        return 1
    review = cmd_review(args)
    if review["decision"] == "revise":
        args.extra_turns = getattr(args, "extra_turns", 25)
        cmd_revise(args)
        if harness.Session(args.session).meta()["status"] == "submitted":
            cmd_review(args)
    cmd_publish(args)
    cmd_build(args)
    log("pipeline complete")
    return 0


def cmd_build(args):
    subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py")], check=True)


def cmd_deploy(args):
    cmd_build(args)
    target = os.environ.get("SUBSTRATE_DEPLOY_TARGET")
    if not target:
        log("SUBSTRATE_DEPLOY_TARGET not set; built locally, skipping rsync.")
        return
    subprocess.run(
        ["rsync", "-az", "--delete", os.path.join(ROOT, "site", "public") + "/",
         target], check=True)
    log(f"deployed to {target}")


def cmd_status(args):
    cmd_project_list(args)
    print("== one-shot sessions ==")
    for mp in sorted(glob.glob(os.path.join(harness.WORK_DIR, "*", "meta.json"))):
        with open(mp) as fh:
            m = json.load(fh)
        print(f"  {m['session_id']:<40} {m['status']:<12} turns={m.get('turns_used', 0)}")
    print("== papers ==")
    for mp in sorted(glob.glob(os.path.join(publish_mod.PAPERS_DIR, "*", "meta.yaml"))):
        with open(mp) as fh:
            m = yaml.safe_load(fh)
        print(f"  substrate:{m['id']}  [{m['author_id']}]  {m['title'][:70]}")


def main():
    ap = argparse.ArgumentParser(prog="substrate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("project").add_subparsers(dest="pcmd", required=True)
    q = pp.add_parser("new"); q.add_argument("model"); q.add_argument("--title", default=None)
    q.set_defaults(fn=cmd_project_new)
    q = pp.add_parser("run"); q.add_argument("project")
    q.add_argument("--turns", type=int, default=40); q.add_argument("--minutes", type=int, default=90)
    q.set_defaults(fn=cmd_project_run)
    q = pp.add_parser("list"); q.set_defaults(fn=cmd_project_list)

    q = sub.add_parser("daily")
    q.add_argument("--model", default="gemma4")
    q.add_argument("--turns", type=int, default=40); q.add_argument("--minutes", type=int, default=90)
    q.set_defaults(fn=cmd_daily)

    q = sub.add_parser("stop"); q.set_defaults(fn=cmd_stop)

    q = sub.add_parser("new"); q.add_argument("model")
    q.add_argument("--turns", type=int, default=60); q.add_argument("--minutes", type=int, default=150)
    q.set_defaults(fn=cmd_new)

    for name, fn in [("run", cmd_run), ("review", cmd_review), ("publish", cmd_publish)]:
        q = sub.add_parser(name); q.add_argument("session"); q.set_defaults(fn=fn)

    q = sub.add_parser("revise"); q.add_argument("session")
    q.add_argument("--extra-turns", type=int, default=25); q.set_defaults(fn=cmd_revise)

    q = sub.add_parser("auto"); q.add_argument("model")
    q.add_argument("--turns", type=int, default=60); q.add_argument("--minutes", type=int, default=150)
    q.set_defaults(fn=cmd_auto)

    for name, fn in [("build", cmd_build), ("deploy", cmd_deploy), ("status", cmd_status)]:
        q = sub.add_parser(name); q.set_defaults(fn=fn)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
