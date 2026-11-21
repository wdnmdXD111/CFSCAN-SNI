import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfoptimizer.config import DEFAULTS, load_config
from cfoptimizer.geo import tokens_from_config
from cfoptimizer.speed import token_from_config
from cfoptimizer.util import OptimizerError


class CredentialTests(unittest.TestCase):
    def test_ipinfo_reads_only_file_and_deduplicates_bom_comments_and_examples(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "ipinfo_tokens.txt").write_text(
                "\ufeff# comment\n\nfile-token\nfile-token\nsecond-token\nYOUR_IPINFO_LITE_TOKEN\n",
                encoding="utf-8",
            )
            with patch.object(os, "environ", dict(os.environ, IPINFO_TOKEN="ignored-environment-token")):
                self.assertEqual(tokens_from_config(DEFAULTS["ipinfo"], base), ["file-token", "second-token"])
                (base / "ipinfo_tokens.txt").unlink()
                self.assertEqual(tokens_from_config(DEFAULTS["ipinfo"], base), [])

    def test_worker_password_is_optional_and_file_based(self):
        cfg = copy.deepcopy(DEFAULTS["speed"])
        cfg["mode"] = "worker"
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            with patch.object(os, "environ", dict(os.environ, SPEED_TOKEN="ignored-environment-token")):
                self.assertEqual(token_from_config(cfg, base), "")
                path = base / "speed_token.txt"
                path.write_text("# public endpoint\n\n", encoding="utf-8")
                self.assertEqual(token_from_config(cfg, base), "")
                path.write_text("\ufeff# comment\nfile-password\n", encoding="utf-8")
                self.assertEqual(token_from_config(cfg, base), "file-password")
                cfg["token_file"] = ""
                self.assertEqual(token_from_config(cfg, base), "")

    def test_worker_rejects_multiple_passwords_and_static_does_not_read_them(self):
        cfg = copy.deepcopy(DEFAULTS["speed"])
        cfg["mode"] = "worker"
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "speed_token.txt").write_text("first\nsecond\n", encoding="utf-8")
            with self.assertRaises(OptimizerError):
                token_from_config(cfg, base)
            cfg["mode"] = "static"
            self.assertEqual(token_from_config(cfg, base), "")

    def test_old_environment_fields_are_removed_while_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "zone": {"domain": "zone.invalid"},
                        "sources": ["ips.txt"],
                        "ipinfo": {"token_env": "OLD_IPINFO"},
                        "speed": {"token_env": "OLD_SPEED"},
                        "github": {"token_env": "OLD_GITHUB"},
                    }
                ),
                encoding="utf-8",
            )
            cfg = load_config(path)
            for section in ("ipinfo", "speed", "github"):
                self.assertNotIn("token_env", cfg[section])
            self.assertEqual(cfg["ipinfo"]["token_file"], "ipinfo_tokens.txt")
            self.assertEqual(cfg["speed"]["token_file"], "speed_token.txt")
            self.assertNotIn("token_file", cfg["github"])

    def test_password_path_cannot_overlap_output_or_git_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for token_path in ("ip.txt", "config.json", "ipinfo_tokens.txt", ".git/token.txt"):
                path.write_text(
                    json.dumps(
                        {
                            "zone": {"domain": "zone.invalid"},
                            "sources": ["ips.txt"],
                            "speed": {"token_file": token_path},
                        }
                    ),
                    encoding="utf-8",
                )
                with self.subTest(token_path=token_path), self.assertRaises(OptimizerError):
                    load_config(path)

    def test_optional_password_path_can_be_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "zone": {"domain": "zone.invalid"},
                        "sources": ["ips.txt"],
                        "speed": {"token_file": ""},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_config(path)["speed"]["token_file"], "")
