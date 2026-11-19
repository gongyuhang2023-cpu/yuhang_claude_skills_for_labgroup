import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lf.py"
sys.path.insert(0, str(SCRIPT.parent))
import lf


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.client = self.root / "installed/lab-frontier/scripts/lf.py"
        self.client.parent.mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.client)
        self.config = {"host": "server.example.invalid", "user": "member"}
        self.args_log = self.root / "ssh-args.json"
        self.env = {
            **os.environ,
            "HOME": str(self.root),
            "USERPROFILE": str(self.root),
            "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_SSH_ARGS": str(self.args_log),
        }
        ssh = self.root / "ssh"
        ssh.write_text(f"#!{sys.executable}\n" + """\
import json
import os
from pathlib import Path
import sys

Path(os.environ["FAKE_SSH_ARGS"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print(os.environ.get("FAKE_SSH_STDOUT", '{"ok":true}'))
print("remote progress", file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_SSH_RC", "0")))
""", encoding="utf-8")
        ssh.chmod(0o700)

    def write_config(self, directory):
        path = directory / "spark-advisor" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.config), encoding="utf-8")

    def run_lf(self, *args, stdout='{"ok":true}', returncode=0):
        env = {**self.env, "FAKE_SSH_STDOUT": stdout, "FAKE_SSH_RC": str(returncode)}
        return subprocess.run([sys.executable, str(self.client), *args], env=env, timeout=10,
                              capture_output=True, encoding="utf-8")

    def ssh_args(self):
        return json.loads(self.args_log.read_text(encoding="utf-8"))

    def test_finds_config_in_both_home_skill_directories(self):
        for name in (".claude", ".agents"):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as home:
                self.write_config(Path(home) / name / "skills")
                with (patch.object(lf, "__file__", str(self.client)),
                      patch.object(lf.Path, "home", return_value=Path(home))):
                    self.assertEqual(lf.load_config(), self.config)

    def test_prefers_adjacent_config(self):
        claude = self.root / ".claude" / "skills"
        self.write_config(self.root / ".agents" / "skills")
        self.config["user"] = "adjacent"
        self.write_config(claude)
        with (patch.object(lf, "__file__", str(claude / "lab-frontier/scripts/lf.py")),
              patch.object(lf.Path, "home", return_value=self.root)):
            self.assertEqual(lf.load_config()["user"], "adjacent")

    def test_missing_config_does_not_invoke_ssh(self):
        result = self.run_lf("status")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["reason"], "ssh_config_missing")
        self.assertFalse(self.args_log.exists())

    def test_ssh_args_and_exact_last_json_line(self):
        self.config.update(ssh_key=str(self.root / "key with spaces"), ssh_port=2222,
                           ssh_opts=["-J", "jump.example.invalid"])
        self.write_config(self.root / ".claude" / "skills")
        line = '{"ok": true, "brief": "简报"}'
        result = self.run_lf("brief", "show", "--date", "2026-01-02", "--json",
                             stdout='banner\n{"old": true}\n' + line + "\n\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, line + "\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.ssh_args(), [
            "-o", "BatchMode=yes", "-J", "jump.example.invalid", "-o", "ConnectTimeout=12",
            "-p", "2222", "-i", self.config["ssh_key"], "-T", "-n", "--",
            "member@server.example.invalid",
            "/usr/local/bin/spark-frontier brief show --date 2026-01-02",
        ])

    def test_uses_reachable_lan_and_falls_back_to_host(self):
        self.config["host_lan"] = "lan.example.invalid"
        with patch.object(lf.socket, "create_connection") as connect:
            self.assertIn("member@lan.example.invalid", lf.ssh_args(self.config, ["status"]))
            connect.assert_called_once_with(("lan.example.invalid", 22), timeout=1)
            connect.side_effect = OSError
            self.assertIn("member@server.example.invalid", lf.ssh_args(self.config, ["status"]))

    def test_setup_relay_preserves_json_and_nonzero_exit(self):
        self.write_config(self.root / ".agents" / "skills")
        line = '{"ok":false,"next":{"action":"gitea_login"}}'
        result = self.run_lf("setup", "--preset", "recommended", stdout=line, returncode=1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, line + "\n")
        self.assertEqual(self.ssh_args()[-1], "/usr/local/bin/spark-frontier setup --preset recommended")

    def test_profile_note_is_quoted_as_one_remote_argument(self):
        self.write_config(self.root / ".agents" / "skills")
        note = "研究方向：'method'; $(unrelated)\n第二行"
        result = self.run_lf("profile", "note", "--person", note)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(shlex.split(self.ssh_args()[-1]),
                         ["/usr/local/bin/spark-frontier", "profile", "note", "--person", note])

    def test_interactive_commands_only_print_a_terminal_command(self):
        self.config["ssh_opts"] = ["-o", "BatchMode=yes"]
        self.write_config(self.root / ".agents" / "skills")
        commands = [
            (("gitea-login",), "/usr/local/bin/spark-frontier gitea-login"),
            (("codex-login", "--binary", "/opt/bin/codex"), "/opt/bin/codex login --device-auth"),
        ]
        for args, remote in commands:
            with self.subTest(args=args):
                result = self.run_lf(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                printed = shlex.split(result.stdout)
                self.assertEqual(printed[0:3], ["ssh", "-o", "BatchMode=no"])
                self.assertEqual(printed[-4:], ["-t", "--", "member@server.example.invalid", remote])
                self.assertNotIn("-n", printed)
                self.assertFalse(self.args_log.exists())

    def test_invalid_final_line_is_not_replaced_by_earlier_json(self):
        self.write_config(self.root / ".agents" / "skills")
        result = self.run_lf("status", stdout='{"ok":true}\ntrailing text')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["reason"], "invalid_response")


if __name__ == "__main__":
    unittest.main()
