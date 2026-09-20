#!/usr/bin/env python3
"""Prepare and run a private, loopback-only we-mp-rss collector (stdlib CLI)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UPSTREAM = "https://github.com/rachelos/we-mp-rss.git"
COMMIT = "d8feb6a42c6773d7374e03c487d3ae3426084af8"
PATCH_VERSION = 1
ENV_ALLOWLIST = {
    "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "TMPDIR", "HOME",
    "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMDATA", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "SYSTEMDRIVE",
}
PATCHES = {
    "main.py": [
        ('    print("环境变量:")\n    for k,v in os.environ.items():\n'
         '        print(f"{k}={v}")\n', "", 1),
        ('host="0.0.0.0"', 'host="127.0.0.1"', 2),
    ],
    "init_sys.py": [
        ('username,password=os.getenv("USERNAME", "admin"),os.getenv("PASSWORD", "admin@123")',
         'username,password=os.environ["WERSS_LOCAL_ADMIN_USER"],os.environ["WERSS_LOCAL_ADMIN_PASSWORD"]', 1),
    ],
    "core/base.py": [
        ("requests.get('https://api.github.com/repos/rachelos/we-mp-rss/releases/latest')",
         "requests.get('https://api.github.com/repos/rachelos/we-mp-rss/releases/latest', timeout=5)", 1),
    ],
}


class SetupError(RuntimeError):
    pass


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def clean_environment(source=None):
    """Never pass the agent's model keys, proxy credentials or Python hooks."""
    source = os.environ if source is None else source
    env = {key: value for key, value in source.items() if key.upper() in ENV_ALLOWLIST}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
    return env


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                   "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(str(temporary), str(path))


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SetupError("Cannot read JSON file: " + str(path))


def credentials(state, create=False):
    path = state / "login.json"
    if create and not path.exists():
        value = {"username": "local-admin", "password": secrets.token_urlsafe(32),
                 "secret_key": secrets.token_urlsafe(48)}
        try:
            with os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                           "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2)
        except FileExistsError:
            pass
    value = read_json(path)
    if not isinstance(value, dict) or not all(isinstance(value.get(key), str) and value[key]
                                            for key in ("username", "password", "secret_key")):
        raise SetupError("login.json must contain username, password and secret_key strings.")
    if len(value["password"]) < 24 or len(value["secret_key"]) < 48:
        raise SetupError("login.json has weak credentials; use a new state directory.")
    return value


def run(command, state, cwd=None, capture=False, timeout=1800):
    """No shell, no inherited model credentials, no terminal windows on Windows."""
    options = {"cwd": str(cwd) if cwd else None, "env": clean_environment(),
               "timeout": timeout, "stdin": subprocess.DEVNULL,
               "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
    options["env"]["PLAYWRIGHT_BROWSERS_PATH"] = str(state / "browsers")
    try:
        if capture:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        else:
            with (state / "setup.log").open("ab") as log:
                result = subprocess.run(command, stdout=log, stderr=log, **options)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("Command could not finish; inspect setup.log in " + str(state))
    if result.returncode:
        raise SetupError("Command failed; inspect setup.log in " + str(state))
    return result.stdout.decode("utf-8").strip() if capture else ""


def find_python(state, explicit=None):
    candidates = [[explicit]] if explicit else [[sys.executable], ["py", "-3.13"], ["python3.13"], ["python3"]]
    for candidate in candidates:
        try:
            version = run(candidate + ["-c", "import sys; print('%s.%s' % sys.version_info[:2])"],
                          state, capture=True, timeout=15)
        except SetupError:
            continue
        if version == "3.13":
            return candidate
    raise SetupError("Python 3.13 is required. Install it, then pass --python /path/to/python3.13.")


def venv_python(state):
    return state / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def patch_text(relative, pristine):
    text = pristine.replace("\r\n", "\n")
    for old, new, count in PATCHES[relative]:
        if text.count(old) != count:
            raise SetupError("Pinned upstream patch context changed: " + relative)
        text = text.replace(old, new)
    return text


def check_repo(state, apply=False):
    repo = state / "we-mp-rss"
    if run(["git", "rev-parse", "HEAD"], state, cwd=repo, capture=True, timeout=30) != COMMIT:
        raise SetupError("Collector checkout is not the pinned commit; use a new state directory.")
    hashes = {}
    for relative in PATCHES:
        pristine = run(["git", "show", COMMIT + ":" + relative], state,
                       cwd=repo, capture=True, timeout=30).rstrip("\n") + "\n"
        expected = patch_text(relative, pristine)
        target = repo / relative
        actual = target.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n") + "\n"
        if actual == pristine and apply:
            _write_text(target, expected)
        elif actual != expected:
            raise SetupError("Unexpected collector source; refusing to overwrite or start: " + relative)
        hashes[relative] = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    return hashes


def _write_text(path, content):
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def prepare(state, python=None):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not shutil.which("git"):
        raise SetupError("Git is required to download and verify the pinned collector.")
    python_command = find_python(state, python)
    repo = state / "we-mp-rss"
    if not repo.exists():
        emit({"step": "download_collector", "commit": COMMIT, "logs": str(state / "setup.log")})
        run(["git", "clone", "--no-checkout", "--filter=blob:none", UPSTREAM, str(repo)], state)
        run(["git", "checkout", "--detach", COMMIT], state, cwd=repo)
    elif not (repo / ".git").exists():
        raise SetupError("The state/we-mp-rss directory is not a Git checkout; choose another state.")
    hashes = check_repo(state, apply=True)
    credentials(state, create=True)
    config = repo / "config.yaml"
    if not config.exists():
        shutil.copyfile(str(repo / "config.example.yaml"), str(config))
    python_path = venv_python(state)
    new_venv = not python_path.exists()
    if new_venv:
        emit({"step": "create_python_313_environment"})
        run(python_command + ["-m", "venv", str(state / "venv")], state)
    find_python(state, str(python_path))
    requirements = repo / "requirements.txt"
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = state / "prepared.json"
    previous = read_json(marker) if marker.exists() and not new_venv else {}
    if previous.get("requirements_sha256") != fingerprint or previous.get("commit") != COMMIT:
        emit({"step": "install_python_dependencies", "logs": str(state / "setup.log")})
        run([str(python_path), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)], state)
    if previous.get("browser") != "webkit" or previous.get("commit") != COMMIT or not (state / "browsers").exists():
        emit({"step": "install_playwright_webkit", "logs": str(state / "setup.log")})
        # Same private browser cache is used by prepare and start.
        run([str(python_path), "-m", "playwright", "install", "webkit"], state)
    write_json(marker, {"commit": COMMIT, "patch_version": PATCH_VERSION,
                        "patch_sha256": hashes, "requirements_sha256": fingerprint, "browser": "webkit"})
    return {"status": "prepared", "state": str(state), "credentials_file": str(state / "login.json"),
            "next": "start --state <same-directory>"}


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(1)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def status(state, port=None):
    process_path = state / "process.json"
    process = read_json(process_path) if process_path.exists() else {}
    port = port or process.get("port", 8006)
    base = "http://127.0.0.1:" + str(port)
    result = {"status": "stopped", "url": base, "state": str(state), "port": port}
    if not listening(port):
        return result
    result["status"] = "port_in_use_unverified"
    try:
        login = credentials(state)
        form = urllib.parse.urlencode({key: login[key] for key in ("username", "password")}).encode("utf-8")
        request = urllib.request.Request(base + "/api/v1/wx/auth/login", data=form,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        # Never send local credentials through an inherited HTTP proxy or a redirect.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=4) as response:
            data = json.load(response)
        if data.get("code", 0) == 0 and data.get("data", data).get("access_token"):
            result["status"] = "running"
            result["credentials_file"] = str(state / "login.json")
    except (SetupError, OSError, ValueError, TypeError, AttributeError, urllib.error.URLError):
        pass
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def child_environment(state, port):
    login = credentials(state)
    env = clean_environment()
    env.update({"WERSS_LOCAL_ADMIN_USER": login["username"], "WERSS_LOCAL_ADMIN_PASSWORD": login["password"],
                "SECRET_KEY": login["secret_key"], "PORT": str(port), "ENABLE_JOB": "False",
                "AUTO_RELOAD": "False", "GATHER.CONTENT_AUTO_CHECK": "False", "GATHER.CONTENT": "True",
                "GATHER.MODEL": "web", "WE_RSS.AUTH": "False", "CASCADE_ENABLED": "False",
                "REDIS_SERVER_ENABLED": "False", "WEREAD_AUTO_ADD_TO_SHELF": "False",
                "ARTICLE_STATS_REFRESH_ENABLED": "False", "BROWSER_TYPE": "webkit", "HEADLESS": "true",
                "PLAYWRIGHT_BROWSERS_PATH": str(state / "browsers")})
    return env


def start(state, port=8006, wait_seconds=25):
    if listening(port):
        current = status(state, port)
        if current["status"] == "running":
            current["status"] = "already_running"
            return current
        raise SetupError("Port is occupied by an unverified service. Inspect it or choose another --port.")
    marker = state / "prepared.json"
    if not marker.exists() or not venv_python(state).exists():
        raise SetupError("Run prepare --state <same-directory> before start.")
    if read_json(marker).get("patch_version") != PATCH_VERSION:
        raise SetupError("Run prepare again to apply this script's collector patches.")
    check_repo(state)
    repo = state / "we-mp-rss"
    options = {"cwd": str(repo), "env": child_environment(state, port), "stdin": subprocess.DEVNULL,
               "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
    if os.name != "nt":
        options["start_new_session"] = True
    with (state / "stdout.log").open("ab") as stdout, (state / "stderr.log").open("ab") as stderr:
        child = subprocess.Popen([str(venv_python(state)), "-u", "main.py", "-init", "True", "-job", "False"],
                                 stdout=stdout, stderr=stderr, **options)
    write_json(state / "process.json", {"pid": child.pid, "port": port, "repo": str(repo)})
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise SetupError("Collector exited during startup. Inspect stderr.log and stdout.log in " + str(state))
        result = status(state, port)
        if result["status"] == "running":
            result.update(pid=child.pid, logs=str(state))
            return result
        time.sleep(0.5)
    return {"status": "starting_unverified", "pid": child.pid, "url": "http://127.0.0.1:" + str(port),
            "logs": str(state), "next": "Run status; process creation alone does not confirm readiness."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "start", "status"):
        command = sub.add_parser(action)
        command.add_argument("--state", required=True, type=Path, help="Private runtime directory outside the shared Skill")
        if action == "prepare":
            command.add_argument("--python", help="Path to a Python 3.13 executable")
        else:
            command.add_argument("--port", type=int, default=8006 if action == "start" else None)
    args = parser.parse_args(argv)
    state = args.state.expanduser().resolve()
    if getattr(args, "port", None) is not None and not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    try:
        if args.action == "prepare":
            result = prepare(state, args.python)
        elif args.action == "start":
            result = start(state, args.port)
        else:
            result = status(state, args.port)
        emit(result)
        return 0 if result["status"] in ("prepared", "running", "already_running") else 2
    except (SetupError, OSError) as error:
        emit({"status": "failed", "message": str(error)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
