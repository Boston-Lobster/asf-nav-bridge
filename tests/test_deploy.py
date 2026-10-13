"""Offline deployment tests: real nginx injector and mocked PowerShell commands."""

import importlib.util
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("inject_nginx", ROOT / "inject_nginx.py")
inject_nginx = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inject_nginx
SPEC.loader.exec_module(inject_nginx)


class NginxInjectorTests(unittest.TestCase):
    def write_conf(self, directory, content):
        path = Path(directory) / "site.conf"
        path.write_text(content, encoding="utf-8")
        return path

    def test_tokenizer_skips_quoted_and_commented_braces(self):
        text = '''
http {
    map $http_user_agent $target {
        default "a } ; { # still quoted";
        # a comment with } { ;
        "~^literal#hash" "value { }";
    }
    server {
        server_name example.test;
        location / {
            if ($arg_x = "}){#") { return 204; }
        }
        listen [::]:443 ssl;
    }
}
'''
        servers, _, _ = inject_nginx.find_https_servers(text)
        self.assertEqual(len(servers), 1)
        self.assertEqual(inject_nginx._server_names(servers[0]), ["example.test"])
        self.assertEqual(
            [item[1].value for item in inject_nginx.direct_directives(servers[0]) if item[0].value == "listen"],
            ["[::]:443"],
        )

    def test_stream_server_is_not_mistaken_for_https_http_server(self):
        text = '''
stream {
    server {
        listen 443;
        proxy_pass backend;
    }
}
http {
    server {
        listen 443 ssl;
        server_name web.example;
    }
}
'''
        servers, _, _ = inject_nginx.find_https_servers(text)
        self.assertEqual(len(servers), 1)
        self.assertEqual(inject_nginx._server_names(servers[0]), ["web.example"])

    def test_domain_selects_correct_server_with_listen_before_or_after_name(self):
        text = '''
server {
    listen 443 ssl;
    server_name first.example;
}

server {
    server_name target.example;
    listen [2001:db8::1]:443 ssl http2;
    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            conf = self.write_conf(directory, text)
            changed, removed, _ = inject_nginx.inject(
                str(conf), ["target.example"], 17055
            )
            result = conf.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertEqual(removed, 0)
        first, second = inject_nginx.find_https_servers(result)[0]
        self.assertNotIn("# asf-bridge-location", result[first.start:first.end])
        second_text = result[second.start:second.end]
        self.assertIn("proxy_pass http://127.0.0.1:17055;", second_text)
        self.assertEqual(second_text.count("location /asf/ {"), 1)
        self.assertLess(second_text.index("location /asf/ {"), second_text.rfind("}"))

    def test_domain_matching_supports_nginx_wildcards(self):
        self.assertTrue(inject_nginx._domain_matches("*.example.test", "bot.example.test"))
        self.assertTrue(inject_nginx._domain_matches(".example.test", "example.test"))
        self.assertFalse(inject_nginx._domain_matches("*.example.test", "example.test"))

    def test_multiple_443_servers_fail_closed_without_matching_domain(self):
        text = '''
server { listen 443 ssl; server_name one.example; }
server { listen [::]:443 ssl; server_name two.example; }
'''
        with tempfile.TemporaryDirectory() as directory:
            conf = self.write_conf(directory, text)
            original = conf.read_text(encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "specify --domain"):
                inject_nginx.inject(str(conf))
            self.assertEqual(conf.read_text(encoding="utf-8"), original)
            self.assertFalse(Path(str(conf) + ".bak-asf-bridge").exists())

    def test_invalid_port_fails_before_changing_config(self):
        text = "server {\n    listen 443 ssl;\n    server_name example.test;\n}\n"
        with tempfile.TemporaryDirectory() as directory:
            conf = self.write_conf(directory, text)
            with self.assertRaisesRegex(ValueError, "1 to 65535"):
                inject_nginx.inject(str(conf), ["example.test"], 70000)
            self.assertEqual(conf.read_text(encoding="utf-8"), text)

    def test_domain_matching_multiple_server_blocks_fails_closed(self):
        text = '''
server { listen 443 ssl; server_name same.example; }
server { listen 443 ssl; server_name same.example; }
'''
        with tempfile.TemporaryDirectory() as directory:
            conf = self.write_conf(directory, text)
            with self.assertRaisesRegex(ValueError, "multiple 443 server blocks"):
                inject_nginx.inject(str(conf), ["same.example"])

    def test_removes_misplaced_marked_block_then_inserts_at_server_level(self):
        text = '''
server {
    listen 443 ssl;
    server_name example.test;
    location / {
        # asf-bridge-location
        location /asf/ {
            proxy_pass http://127.0.0.1:17001;
        }
        proxy_pass http://127.0.0.1:8080;
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            conf = self.write_conf(directory, text)
            changed, removed, _ = inject_nginx.inject(str(conf), ["example.test"])
            result = conf.read_text(encoding="utf-8")
            server = inject_nginx.find_https_servers(result)[0][0]
            direct_locations = [
                block
                for block in server.items
                if isinstance(block, inject_nginx.Block)
                and block.header
                and block.header[0].value == "location"
            ]

        self.assertTrue(changed)
        self.assertEqual(removed, 1)
        self.assertEqual(sum(block.header[1].value == "/asf/" for block in direct_locations), 1)
        self.assertEqual(result.count("# asf-bridge-location"), 1)
        self.assertIn("location / {", result)

    def test_injection_is_idempotent_and_preserves_first_backup(self):
        text = "server {\n    listen 443 ssl;\n    server_name example.test;\n}\n"
        with tempfile.TemporaryDirectory() as directory:
            conf = self.write_conf(directory, text)
            backup = Path(str(conf) + ".bak-asf-bridge")
            self.assertTrue(inject_nginx.inject(str(conf), ["example.test"])[0])
            first = conf.read_text(encoding="utf-8")
            self.assertEqual(backup.read_text(encoding="utf-8"), text)
            changed, removed, _ = inject_nginx.inject(str(conf), ["example.test"])
            self.assertFalse(changed)
            self.assertEqual(removed, 1)
            self.assertEqual(conf.read_text(encoding="utf-8"), first)
            self.assertEqual(backup.read_text(encoding="utf-8"), text)


@unittest.skipUnless(shutil.which("bash"), "Bash is required")
class LinuxRollbackTests(unittest.TestCase):
    def run_sandbox_deploy(self, invalid_config=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging, dest, fake_bin = root / "staging", root / "dest", root / "bin"
            staging.mkdir()
            fake_bin.mkdir()
            for filename in ("asf-bridge.py", "inject_nginx.py"):
                shutil.copy2(ROOT / filename, staging / filename)
            for filename in ("nav-asf-snippet.html", "asf-bridge.service"):
                (staging / filename).write_text("fixture", encoding="utf-8")
            (staging / "inject_nav.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
            config = {"listen_host": "127.0.0.1", "listen_port": 17401,
                      "asf_ipc_url": "http://127.0.0.1:17442", "asf_ipc_password": "fixture",
                      "bot_name": "Fixture", "play_appids": [] if invalid_config else [123],
                      "ui_token": "fixture-token", "allowed_origins": ["example.test"]}
            (staging / "config.json").write_text(json.dumps(config), encoding="utf-8")
            conf = root / "nginx.conf"
            original = "server { listen 443; server_name example.test; }\n"
            conf.write_text(original, encoding="utf-8")
            calls = root / "calls.log"
            for name in ("nginx", "systemctl"):
                executable = fake_bin / name
                executable.write_text('#!/bin/bash\nprintf "%s\\n" "' + name + ' $*" >> "$FAKE_LOG"\nexit 0\n', encoding="utf-8")
                executable.chmod(0o755)
            # Only installation paths are redirected. Execute the real control flow,
            # using temporary files and fake service commands.
            script_text = (ROOT / "deploy.sh").read_text(encoding="utf-8")
            script_text = script_text.replace("/opt/asf-bridge-staging", str(staging))
            script_text = script_text.replace("/opt/asf-bridge", str(dest))
            script_text = script_text.replace("/etc/systemd/system/asf-bridge.service", str(root / "service"))
            script = root / "deploy.sh"
            script.write_text(script_text, encoding="utf-8")
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
                       ASF_NGINX_CONF=str(conf), ASF_NAV_DOMAIN="example.test", FAKE_LOG=str(calls))
            result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=30)
            recorded = calls.read_text().splitlines() if calls.exists() else []
            return result, recorded, conf.read_text(), original, dest.exists()

    def test_post_reload_failure_restores_and_reloads_original_nginx(self):
        result, calls, changed, original, _ = self.run_sandbox_deploy()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(changed, original)
        self.assertEqual(calls.count("systemctl reload nginx"), 2)

    def test_invalid_bridge_config_stops_before_installation(self):
        result, calls, changed, original, installed = self.run_sandbox_deploy(invalid_config=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(installed)
        self.assertEqual(calls, [])
        self.assertEqual(changed, original)


class ExistingConfigurationTests(unittest.TestCase):
    def test_unmarked_asf_location_is_preserved_and_rejected(self):
        for location in ("/asf/", "^~ /asf/", "/asf", "^~ /asf"):
            original = "server { listen 443 ssl; server_name example.test; location " + location + " { return 403; } }"
            with tempfile.TemporaryDirectory() as directory:
                conf = Path(directory) / "nginx.conf"
                conf.write_text(original, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "unmarked"):
                    inject_nginx.inject(str(conf), ["example.test"])
                self.assertEqual(conf.read_text(encoding="utf-8"), original)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation may require elevated privileges")
    def test_symlink_and_original_file_mode_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "available.conf"
            link = Path(directory) / "enabled.conf"
            target.write_text("server { listen 443; server_name example.test; }", encoding="utf-8")
            target.chmod(0o644)
            link.symlink_to(target)
            inject_nginx.inject(str(link), ["example.test"])
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertIn("location /asf/", target.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell is required")
class PowerShellDeployTests(unittest.TestCase):
    def run_script_with_fake_commands(self, fail_command="", known_hosts_present=True):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            script = bundle / "deploy.ps1"
            shutil.copy2(ROOT / "deploy.ps1", script)
            for filename in (
                "asf-bridge.py",
                "config.json",
                "nav-asf-snippet.html",
                "inject_nav.py",
                "inject_nginx.py",
                "asf-bridge.service",
                "deploy.sh",
            ):
                (bundle / filename).write_text("stub", encoding="utf-8")
            key = bundle / "key"
            known_hosts = bundle / "known_hosts"
            key.write_text("stub", encoding="utf-8")
            if known_hosts_present:
                known_hosts.write_text("verified-host-key\n", encoding="utf-8")
            log = bundle / "calls.log"

            prelude = f'''
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$global:SSH_CALL_COUNT = 0
function ssh {{
  $global:SSH_CALL_COUNT++
  Add-Content -LiteralPath '{log}' -Value ("ssh|" + ($args -join '|'))
  $fail = ($env:FAKE_FAIL_COMMAND -eq 'ssh') -or (($env:FAKE_FAIL_COMMAND -eq 'ssh-last') -and ($global:SSH_CALL_COUNT -eq 2))
  $global:LASTEXITCODE = if ($fail) {{ 11 }} else {{ 0 }}
}}
function scp {{
  Add-Content -LiteralPath '{log}' -Value ("scp|" + ($args -join '|'))
  $global:LASTEXITCODE = if ($env:FAKE_FAIL_COMMAND -eq 'scp') {{ 22 }} else {{ 0 }}
}}
try {{ & '{script}'; exit 0 }} catch {{ [Console]::Error.WriteLine($_); exit 42 }}
'''
            env = os.environ.copy()
            env.update(
                {
                    "ASF_SERVER_IP": "192.0.2.7",
                    "ASF_SSH_KEY": str(key),
                    "ASF_SSH_KNOWN_HOSTS": str(known_hosts),
                    "ASF_NAV_DOMAIN": "https://example.test",
                    "ASF_NGINX_CONF": "/etc/nginx/sites-enabled/example.test",
                    "FAKE_FAIL_COMMAND": fail_command,
                }
            )
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    prelude,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=30,
            )
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            return result, calls

    def test_powershell_checks_all_native_commands_and_keeps_strict_host_key(self):
        result, calls = self.run_script_with_fake_commands()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line.split("|", 1)[0] for line in calls], ["ssh", "scp", "ssh"])
        for line in calls:
            self.assertIn("BatchMode=yes", line)
            self.assertIn("StrictHostKeyChecking=yes", line)
            self.assertIn("UserKnownHostsFile=", line)
        remote_command = calls[-1].rsplit("|", 1)[-1]
        encoded_overrides = remote_command.rsplit("'", 2)[1]
        overrides = json.loads(base64.b64decode(encoded_overrides))
        self.assertEqual(overrides["ASF_NAV_DOMAIN"], "https://example.test")
        self.assertEqual(overrides["ASF_NGINX_CONF"], "/etc/nginx/sites-enabled/example.test")

    def test_scp_failure_throws_before_remote_deploy(self):
        result, calls = self.run_script_with_fake_commands("scp")
        self.assertEqual(result.returncode, 42)
        self.assertIn("SCP upload failed", result.stderr)
        self.assertEqual([line.split("|", 1)[0] for line in calls], ["ssh", "scp"])

    def test_initial_ssh_failure_throws_before_upload(self):
        result, calls = self.run_script_with_fake_commands("ssh")
        self.assertEqual(result.returncode, 42)
        self.assertIn("SSH staging setup failed", result.stderr)
        self.assertEqual([line.split("|", 1)[0] for line in calls], ["ssh"])

    def test_remote_ssh_failure_is_checked(self):
        result, calls = self.run_script_with_fake_commands("ssh-last")
        self.assertEqual(result.returncode, 42)
        self.assertIn("Remote deployment failed", result.stderr)
        self.assertEqual([line.split("|", 1)[0] for line in calls], ["ssh", "scp", "ssh"])

    def test_first_host_key_requires_verified_known_hosts_entry(self):
        result, calls = self.run_script_with_fake_commands(known_hosts_present=False)
        self.assertEqual(result.returncode, 42)
        self.assertIn("不会自动信任首次连接的主机", result.stderr)
        self.assertEqual(calls, [])


class ShellDeployStaticTests(unittest.TestCase):
    def test_shell_deploy_rolls_back_nginx_and_checks_authenticated_status(self):
        source = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        nginx_test = source.index("if ! nginx -t; then")
        nginx_test_body = source[nginx_test:source.index("\nfi\n", nginx_test)]

        self.assertIn('cp -p "$NGINX_CONF" "$ROLLBACK_BACKUP"', source)
        self.assertIn("restore_nginx_config", nginx_test_body)
        self.assertIn("ASF_NGINX_CONF:-/etc/nginx/sites-available/YOUR_SITE", source)
        self.assertIn("still uses the YOUR_SITE template", source)
        self.assertIn('get("listen_port", 17001)', source)
        self.assertIn('get("allowed_origins", [])', source)
        self.assertIn('"${DOMAIN_ARGS[@]}"', source)
        self.assertLess(
            source.index("systemctl enable asf-bridge"),
            source.index("systemctl restart asf-bridge"),
        )
        self.assertGreaterEqual(source.count("--fail --silent --show-error"), 2)
        self.assertGreaterEqual(source.count('"X-UI-Token: $TOKEN"'), 2)
        self.assertIn('data.get("asf_reachable") is True', source)


if __name__ == "__main__":
    unittest.main()
