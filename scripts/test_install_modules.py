import gzip
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import sys
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("installer", Path(__file__).with_name("install_modules.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ModuleInstallerTests(unittest.TestCase):
    def test_matching_source_pin_is_reused_without_checkout_or_fetch(self):
        source = {"url": "https://github.com/example/module.git", "commit": "a" * 40}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(installer, "run", side_effect=[source["url"], "", source["commit"]]) as run:
                installer.fetch_source(Path(directory), source)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any("fetch" in command or "checkout" in command for command in commands))

    def test_command_output_can_be_redirected_to_a_migration_log(self):
        with tempfile.TemporaryFile() as log:
            installer.run([sys.executable, "-c", "print('migration output')"], stdout=log)
            log.seek(0)
            self.assertEqual(log.read(), b"migration output\n")

    def test_auction_name_is_sql_encoded_and_bot_accounts_are_rejected(self):
        malicious_name = "O'Brien'; DROP DATABASE acore_auth;--"
        with patch.object(installer, "query", return_value="42\t0\t0") as query:
            self.assertEqual(installer.auction_guid(Path("."), malicious_name), 42)
            sql = query.call_args.args[1]
            self.assertNotIn(malicious_name, sql)
            self.assertIn(malicious_name.encode().hex(), sql)
        for result in ("42\t1\t0", "42\t0\t1", "42\t0\t2", ""):
            with patch.object(installer, "query", return_value=result):
                with self.assertRaises(ValueError):
                    installer.auction_guid(Path("."), "Auctioneer")

    def test_dump_requires_all_databases_and_completion_footer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dump.sql.gz"
            data = b"\xff\x00" + b"\n".join(
                f"-- Current Database: `{name}`".encode() for name in installer.DATABASES
            )
            for contents in (data, data.replace(b"acore_auth", b"missing") + b"\n-- Dump completed on now"):
                with gzip.open(path, "wb") as stream:
                    stream.write(contents)
                with self.assertRaises(ValueError):
                    installer.verify_dump(path)
            with gzip.open(path, "wb") as stream:
                stream.write(data + b"\n-- Dump completed on now")
            installer.verify_dump(path)

    def test_backup_compresses_sql_and_records_exact_images_and_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Path(directory)
            (server / "config").mkdir()
            (server / "config/worldserver.conf").write_text("EnablePlayerSettings = 0\n")
            (server / "compose.yaml").write_text("name: test\n")
            (server / ".env").write_text("MYSQL_ROOT_PASSWORD=private-fixture\n")
            sources = server / "source"
            (sources / "modules").mkdir(parents=True)
            (sources / "data").mkdir()
            mounts = [
                {"Type": "volume", "Name": "test-data", "Destination": "/azerothcore/env/dist/data"},
                {"Type": "bind", "Source": str(sources / "modules"), "Destination": "/azerothcore/modules"},
                {"Type": "bind", "Source": str(sources / "data"), "Destination": "/azerothcore/data"},
            ]
            containers = {name: {"Id": name, "Image": f"sha256:{name}", "Mounts": mounts}
                          for name in ("ac-worldserver", "ac-authserver", "ac-database")}
            model = {"services": {name: {"image": "old-tag"} for name in containers}}
            sql = b"\xff" + b"\n".join(f"-- Current Database: `{name}`".encode() for name in installer.DATABASES)
            sql += b"\n-- Dump completed on now\n"
            process = subprocess.Popen([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({sql!r})"],
                                       stdout=subprocess.PIPE)

            def docker_archive(args, **kwargs):
                if args[1:3] == ["image", "save"]:
                    Path(args[args.index("--output") + 1]).write_bytes(b"fixture image export")
                else:
                    bind = next(arg for arg in args if arg.startswith("type=bind,src="))
                    folder = Path(bind.split("src=", 1)[1].split(",dst=", 1)[0])
                    with tarfile.open(folder / "client-data.tar.gz", "w:gz") as archive:
                        archive.add(server / "config/worldserver.conf", arcname="dbc/fixture")

            with patch.object(installer.subprocess, "Popen", return_value=process) as dump, \
                 patch.object(installer, "run", side_effect=docker_archive):
                destination = installer.backup(server, model, containers)
            self.assertIn("--hex-blob", dump.call_args.args[0][-1])
            self.assertIn("--add-drop-database", dump.call_args.args[0][-1])
            self.assertTrue((destination / "COMPLETE").exists())
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual((destination / "databases.sql.gz").read_bytes()[:2], b"\x1f\x8b")
            with gzip.open(destination / "databases.sql.gz", "rb") as stream:
                self.assertEqual(stream.read(), sql)
            previous = json.loads((destination / "previous.compose.json").read_text())
            self.assertEqual(previous["services"]["ac-worldserver"]["image"], "sha256:ac-worldserver")
            with tarfile.open(destination / "deployment-files.tar.gz") as archive:
                self.assertIn(".env", archive.getnames())
            for line in (destination / "SHA256SUMS").read_text().splitlines():
                digest, filename = line.split("  ", 1)
                self.assertEqual(hashlib.sha256((destination / filename).read_bytes()).hexdigest(), digest)

    def test_backup_failure_never_changes_configuration_or_runs_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Path(directory)
            calls = []
            with patch.object(installer, "compose", side_effect=lambda *a, **k: calls.append(a)), \
                 patch.object(installer, "backup", side_effect=RuntimeError("disk full")), \
                 patch.object(installer, "configure_modules") as configure:
                with self.assertRaises(RuntimeError):
                    installer.install(server, "Auctioneer", {}, {}, Path("core"), {})
            configure.assert_not_called()
            self.assertFalse((server / "compose.override.yaml").exists())
            self.assertTrue(all(args[1] == "stop" for args in calls))

    def test_migration_failure_leaves_services_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Path(directory)
            calls = []

            def compose(*args, **kwargs):
                calls.append(args)
                if args[1] == "run":
                    raise RuntimeError("SQL migration failed")

            with patch.object(installer, "compose", side_effect=compose), \
                 patch.object(installer, "backup", return_value=server), \
                 patch.object(installer, "auction_guid", return_value=42), \
                 patch.object(installer, "configure_modules"):
                with self.assertRaises(RuntimeError):
                    installer.install(server, "Auctioneer", {}, {}, Path("core"),
                                      {"worldserver": "new-world", "authserver": "new-auth"})
            self.assertFalse(any(args[1] == "up" for args in calls))
            self.assertEqual(calls[-1][1], "stop")

    def test_success_requires_backup_then_migration_then_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Path(directory)
            calls = []

            def compose(*args, **kwargs):
                calls.append(args[1])
                if args[1] == "run":
                    kwargs["stdout"].write(b"AzerothCore Dry Run Completed, Terminating.\n")

            def backup(*args):
                calls.append("backup")
                return server

            def configure(*args):
                self.assertIn("backup", calls)
                calls.append("configure")

            with patch.object(installer, "compose", side_effect=compose), \
                 patch.object(installer, "backup", side_effect=backup), \
                 patch.object(installer, "auction_guid", return_value=42), \
                 patch.object(installer, "configure_modules", side_effect=configure), \
                 patch.object(installer, "query"):
                installer.install(server, "Auctioneer", {}, {}, Path("core"),
                                  {"worldserver": "new-world", "authserver": "new-auth"})
            self.assertLess(calls.index("stop"), calls.index("backup"))
            self.assertLess(calls.index("backup"), calls.index("configure"))
            self.assertLess(calls.index("run"), calls.index("up"))
            self.assertLess(calls.index("up"), calls.index("exec"))

    def test_zero_exit_without_migration_completion_does_not_start_server(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Path(directory)
            calls = []
            with patch.object(installer, "compose", side_effect=lambda *a, **k: calls.append(a[1])), \
                 patch.object(installer, "backup", return_value=server), \
                 patch.object(installer, "auction_guid", return_value=42), \
                 patch.object(installer, "configure_modules"):
                with self.assertRaisesRegex(RuntimeError, "dry-run completion"):
                    installer.install(server, "Auctioneer", {}, {}, Path("core"),
                                      {"worldserver": "new-world", "authserver": "new-auth"})
            self.assertNotIn("up", calls)

    def test_wrath_start_uses_shared_module_configuration_only(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Path(directory)
            target = server / "config/modules"
            target.mkdir(parents=True)
            (target / "individualProgression.conf").write_text(
                "IndividualProgression.StartingProgression = 0\n"
                "IndividualProgression.tbcRacesStartingProgression = 0\n"
                "IndividualProgression.DeathKnightStartingProgression = 13\n"
                "IndividualProgression.ProgressionLimit = 16\n"
            )
            (target / "mod_ahbot.conf").write_text("AuctionHouseBot.ItemsPerCycle = 25\n")
            (target / "playerbots.conf").write_text("Playerbots.MaxRandomBots = 2500\n")
            installer.configure_modules(server)
            text = (target / "individualProgression.conf").read_text()
            for key in ("StartingProgression", "tbcRacesStartingProgression", "DeathKnightStartingProgression"):
                self.assertIn(f"IndividualProgression.{key} = 13", text)
            self.assertIn("ProgressionLimit = 0", text)
            self.assertIn("ItemsPerCycle = 150", (target / "mod_ahbot.conf").read_text())
            self.assertEqual((target / "playerbots.conf").read_text(), "Playerbots.MaxRandomBots = 2500\n")

    def test_overlay_migrates_without_bots_and_preserves_public_ports(self):
        result = installer.overlay(Path("/sources/core"), {"worldserver": "world", "authserver": "auth"}, 42)
        world = result["services"]["ac-worldserver"]
        migrate = result["services"]["migrate"]
        self.assertNotIn("ports", world)
        self.assertEqual(world["environment"]["AC_UPDATES_ENABLE_DATABASES"], "0")
        self.assertEqual(world["environment"]["AC_AUCTION_HOUSE_BOT_GUIDS"], "42")
        self.assertEqual(world["environment"]["AC_ENABLE_PLAYER_SETTINGS"], "1")
        self.assertEqual(migrate["environment"]["AC_UPDATES_ENABLE_DATABASES"], "7")
        self.assertEqual(migrate["environment"]["AC_AUCTION_HOUSE_BOT_ENABLE_SELLER"], "0")
        self.assertEqual(migrate["environment"]["AC_AUCTION_HOUSE_BOT_BUYER_ENABLED"], "0")


if __name__ == "__main__":
    unittest.main()
