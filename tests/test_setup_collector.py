import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_collector.py"
SPEC = importlib.util.spec_from_file_location("setup_collector", SCRIPT)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_environment_removes_agent_secrets_and_python_hooks(self):
        source = {"PATH": "tool-path", "SYSTEMROOT": "system", "HOME": "private-home",
                  "OPENAI_API_KEY": "secret-openai", "ANTHROPIC_API_KEY": "secret-anthropic",
                  "AZURE_OPENAI_KEY": "secret-azure", "HTTP_PROXY": "http://user:pass@proxy",
                  "PYTHONPATH": "injected", "PYTHONSTARTUP": "injected", "PASSWORD": "legacy"}
        env = setup.clean_environment(source)
        self.assertEqual(env["PATH"], "tool-path")
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertFalse(any("secret" in value or "injected" in value or "user:pass" in value for value in env.values()))
        self.assertNotIn("PASSWORD", env)

    def test_credentials_are_stable_unique_and_private(self):
        first = setup.credentials(self.state, create=True)
        second = setup.credentials(self.state, create=True)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first["password"]), 32)
        other = self.state / "other"
        other.mkdir()
        self.assertNotEqual(first["password"], setup.credentials(other, create=True)["password"])
        if os.name != "nt":
            self.assertEqual((self.state / "login.json").stat().st_mode & 0o777, 0o600)

    def test_weak_existing_credentials_are_not_replaced(self):
        path = self.state / "login.json"
        path.write_text(json.dumps({"username": "admin", "password": "admin", "secret_key": "bad"}))
        before = path.read_bytes()
        with self.assertRaises(setup.SetupError):
            setup.credentials(self.state, create=True)
        self.assertEqual(path.read_bytes(), before)

    def test_patches_are_exact_and_disable_network_bind_and_env_dump(self):
        pristine = 'import os\n' + setup.PATCHES["main.py"][0][0] + '\na(host="0.0.0.0")\nb(host="0.0.0.0")\n'
        result = setup.patch_text("main.py", pristine.replace("\n", "\r\n"))
        self.assertNotIn("os.environ.items()", result)
        self.assertNotIn("0.0.0.0", result)
        self.assertEqual(result.count('host="127.0.0.1"'), 2)
        with self.assertRaises(setup.SetupError):
            setup.patch_text("main.py", pristine.replace('b(host="0.0.0.0")', ""))

    def test_patch_is_idempotent_and_unknown_modifications_are_preserved(self):
        repo = self.state / "we-mp-rss"
        pristine = {}
        for relative, replacements in setup.PATCHES.items():
            pristine[relative] = "\n".join(old * count for old, _, count in replacements) + "\n"
            file = repo / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(pristine[relative], encoding="utf-8")

        def fake_run(command, *args, **kwargs):
            if command[1] == "rev-parse":
                return setup.COMMIT
            return pristine[command[2].split(":", 1)[1]].strip()

        # git show's output retains indentation; the real main file starts with imports.
        for relative in pristine:
            pristine[relative] = "# pinned fixture\n" + pristine[relative]
            (repo / relative).write_text(pristine[relative], encoding="utf-8")
        with patch.object(setup, "run", side_effect=fake_run):
            first = setup.check_repo(self.state, apply=True)
            self.assertEqual(first, setup.check_repo(self.state, apply=True))
            path = repo / "main.py"
            path.write_text(path.read_text(encoding="utf-8") + "# user change\n", encoding="utf-8")
            with self.assertRaises(setup.SetupError):
                setup.check_repo(self.state, apply=True)
            self.assertTrue(path.read_text(encoding="utf-8").endswith("# user change\n"))

    def test_wrong_upstream_commit_is_rejected(self):
        with patch.object(setup, "run", return_value="other-commit"):
            with self.assertRaises(setup.SetupError):
                setup.check_repo(self.state, apply=True)

    def test_child_environment_uses_local_credentials_only(self):
        login = setup.credentials(self.state, create=True)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "do-not-pass", "PASSWORD": "legacy"}):
            env = setup.child_environment(self.state, 8010)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("PASSWORD", env)
        self.assertEqual(env["WERSS_LOCAL_ADMIN_PASSWORD"], login["password"])
        self.assertEqual(env["PORT"], "8010")
        self.assertEqual(env["ENABLE_JOB"], "False")
        self.assertEqual(env["WEREAD_AUTO_ADD_TO_SHELF"], "False")
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], str(self.state / "browsers"))

    def test_runner_uses_minimal_env_no_shell_no_window(self):
        done = subprocess.CompletedProcess(["tool"], 0, stdout=b"3.13\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "do-not-pass"}), patch.object(setup.subprocess, "run", return_value=done) as mock:
            self.assertEqual(setup.run(["tool"], self.state, capture=True), "3.13")
        kwargs = mock.call_args[1]
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def test_occupied_port_never_spawns_or_kills_service(self):
        with patch.object(setup, "listening", return_value=True), patch.object(setup, "status", return_value={"status": "port_in_use_unverified"}), patch.object(setup.subprocess, "Popen") as popen:
            with self.assertRaises(setup.SetupError):
                setup.start(self.state)
            popen.assert_not_called()

    def test_existing_verified_service_is_reused(self):
        with patch.object(setup, "listening", return_value=True), patch.object(setup, "status", return_value={"status": "running"}), patch.object(setup.subprocess, "Popen") as popen:
            self.assertEqual(setup.start(self.state)["status"], "already_running")
            popen.assert_not_called()

    def test_status_of_missing_state_does_not_create_files(self):
        target = self.state / "absent"
        with patch.object(setup, "listening", return_value=False):
            self.assertEqual(setup.status(target)["status"], "stopped")
        self.assertFalse(target.exists())

    def test_redirect_is_rejected(self):
        self.assertIsNone(setup.NoRedirect().redirect_request(None, None, 302, "", {}, "https://external.example"))

    def test_python_version_must_be_313(self):
        with patch.object(setup, "run", return_value="3.12"):
            with self.assertRaises(setup.SetupError):
                setup.find_python(self.state, "python")
        with patch.object(setup, "run", return_value="3.13"):
            self.assertEqual(setup.find_python(self.state, "python"), ["python"])

    def test_prepare_reuses_dependencies_preserves_config_and_credentials(self):
        repo = self.state / "we-mp-rss"
        (repo / ".git").mkdir(parents=True)
        (repo / "requirements.txt").write_text("example==1.0\n")
        (repo / "config.example.yaml").write_text("default: true\n")
        (repo / "config.yaml").write_text("user_config: keep\n")
        python = setup.venv_python(self.state)
        python.parent.mkdir(parents=True)
        python.touch()
        (self.state / "browsers").mkdir()
        with patch.object(setup.shutil, "which", return_value="git"), patch.object(setup, "find_python", return_value=["python3.13"]), patch.object(setup, "check_repo", return_value={"main.py": "reviewed"}), patch.object(setup, "run", return_value="") as run, patch.object(setup, "emit"):
            setup.prepare(self.state)
            first_count = run.call_count
            first_login = (self.state / "login.json").read_bytes()
            self.assertEqual(first_count, 2)
            setup.prepare(self.state)
            self.assertEqual(run.call_count, first_count)
        self.assertEqual((self.state / "login.json").read_bytes(), first_login)
        self.assertEqual((repo / "config.yaml").read_text(), "user_config: keep\n")

    def test_start_reports_unverified_until_health_check_passes(self):
        (self.state / "prepared.json").write_text(json.dumps({"patch_version": setup.PATCH_VERSION}))
        python = setup.venv_python(self.state)
        python.parent.mkdir(parents=True)
        python.touch()
        setup.credentials(self.state, create=True)
        with patch.object(setup, "listening", return_value=False), patch.object(setup, "check_repo"), patch.object(setup.subprocess, "Popen") as popen:
            popen.return_value.pid = 4321
            result = setup.start(self.state, 8020, wait_seconds=0)
        self.assertEqual(result["status"], "starting_unverified")
        command = popen.call_args[0][0]
        self.assertEqual(command[-4:], ["-init", "True", "-job", "False"])
        options = popen.call_args[1]
        self.assertNotIn("shell", options)
        self.assertEqual(options["env"]["PORT"], "8020")
        self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(json.loads((self.state / "process.json").read_text())["pid"], 4321)


if __name__ == "__main__":
    unittest.main()
