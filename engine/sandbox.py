"""Bubblewrap sandbox for Substrate research sessions.

Each session gets a persistent workspace directory bound at /home/researcher.
Everything else is a read-only view of the host OS: no access to the host's
home directory, no inherited environment, no docker required.

The sandbox boundary is filesystem + environment isolation. The network
namespace is shared deliberately: authors get internet access and can reach
the local model APIs (ollama :11434, llama.cpp :8770). See
spec/RESEARCH-INTEGRITY.md, "Known limitations".
"""

import os
import shutil
import subprocess


def _have_prlimit():
    return shutil.which("prlimit") is not None

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
LABENV_HOST = os.path.join(ENGINE_DIR, "labenv")
LABENV_MOUNT = "/opt/labenv"

SANDBOX_HOME = "/home/researcher"
SANDBOX_PATH = f"{SANDBOX_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Curated read-only /etc. Full /etc would drag in host config we don't need
# to expose; this list is what python/curl/pip/git actually require.
# passwd/group are synthesized (see below) rather than bound from the host,
# so a session can't enumerate host accounts.
ETC_RO = [
    "resolv.conf", "hosts", "nsswitch.conf", "ssl", "ca-certificates",
    "localtime", "ld.so.cache", "ld.so.conf", "fonts",
    "ld.so.conf.d", "alternatives", "mime.types", "gai.conf",
]

FAKE_PASSWD = (
    "root:x:0:0:root:/root:/usr/sbin/nologin\n"
    "researcher:x:1000:1000:researcher:/home/researcher:/bin/bash\n"
    "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
)
FAKE_GROUP = "root:x:0:\nresearcher:x:1000:\nnobody:x:65534:\n"

# Host paths under /usr that carry no research value and may hold secrets
# (e.g. NoMachine's world-readable server keys). Masked with an empty tmpfs.
USR_MASKS = ["/usr/NX"]

NVIDIA_DEVICES = [
    "/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-uvm",
    "/dev/nvidia-uvm-tools", "/dev/nvidia-modeset", "/dev/dri",
]

# Per-command resource caps applied on the host side before bwrap. Bounds a
# runaway/fork-bombing session; the per-command timeout bounds wall clock.
PRLIMIT = ["--nproc=512", "--as=12884901888", "--fsize=4294967296"]  # 12G addr, 4G file

_FAKE_ETC = os.path.join(ENGINE_DIR, ".fake-etc")


def _ensure_fake_etc():
    os.makedirs(_FAKE_ETC, exist_ok=True)
    for name, content in (("passwd", FAKE_PASSWD), ("group", FAKE_GROUP)):
        path = os.path.join(_FAKE_ETC, name)
        if not os.path.exists(path) or open(path).read() != content:
            with open(path, "w") as fh:
                fh.write(content)


def bwrap_argv(workspace: str, command: str, *, timeout_kill: bool = True) -> list:
    """Build the bwrap argv to run `command` under bash in the sandbox."""
    argv = [
        "bwrap",
        "--die-with-parent",
        "--unshare-user", "--unshare-pid", "--unshare-ipc",
        "--unshare-uts", "--unshare-cgroup",
        "--uid", "1000", "--gid", "1000",
        "--hostname", "substrate",
        "--clearenv",
        "--setenv", "HOME", SANDBOX_HOME,
        "--setenv", "USER", "researcher",
        "--setenv", "LOGNAME", "researcher",
        "--setenv", "PATH", SANDBOX_PATH,
        "--setenv", "TERM", "dumb",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "PYTHONUNBUFFERED", "1",
        "--setenv", "PYTHONPATH", LABENV_MOUNT,
        "--setenv", "MPLBACKEND", "Agg",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--bind", workspace, SANDBOX_HOME,
        "--chdir", SANDBOX_HOME,
    ]
    # mask host extras under /usr that carry no research value and may hold secrets
    for mask in USR_MASKS:
        if os.path.isdir(mask):
            argv += ["--tmpfs", mask]
    # synthetic passwd/group so a session can't enumerate host accounts
    _ensure_fake_etc()
    argv += ["--ro-bind", os.path.join(_FAKE_ETC, "passwd"), "/etc/passwd"]
    argv += ["--ro-bind", os.path.join(_FAKE_ETC, "group"), "/etc/group"]
    for name in ETC_RO:
        path = os.path.join("/etc", name)
        if os.path.exists(path):
            argv += ["--ro-bind", path, path]
    for dev in NVIDIA_DEVICES:
        if os.path.exists(dev):
            argv += ["--dev-bind", dev, dev]
    if os.path.isdir(LABENV_HOST):
        argv += ["--ro-bind", LABENV_HOST, LABENV_MOUNT]
    argv += ["bash", "-c", command]
    return argv


def run(workspace: str, command: str, timeout: int = 600):
    """Run a command in the sandbox. Returns (exit_code, stdout, stderr, timed_out)."""
    argv = bwrap_argv(workspace, command)
    if _have_prlimit():
        argv = ["prlimit", *PRLIMIT] + argv
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace",
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return 124, out, err, True
