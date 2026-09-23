import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from karpathy_wiki_ingest import health as healthcheck
from karpathy_wiki_ingest.shared import TargetedAnonymizer
from karpathy_wiki_ingest_webdav import (
    ACTIVE_SYMLINK,
    GENERATION_METADATA_FILENAME,
    GENERATIONS_DIRECTORY,
    Settings,
    active_generation,
    install_stop_handler,
    interval_seconds,
    publish_generation,
    run,
    synchronize,
)

ANONYMIZER_CONFIGURATION = {"people": [{"replacement": "[ICH]", "values": ["Max Mustermann"]}]}


def anonymizer():
    return TargetedAnonymizer.from_config(ANONYMIZER_CONFIGURATION)


def active(sanitized: Path) -> Path:
    generation = active_generation(sanitized)
    assert generation is not None
    return generation


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.incoming = self.root / "incoming"
        self.sanitized = self.root / "sanitized"
        self.quarantine = self.root / "quarantine"
        self.incoming.mkdir()

    def publish(self, anonymizer_instance=None):
        return publish_generation(
            self.incoming, self.sanitized, self.quarantine, anonymizer_instance or anonymizer()
        )

    def current_path(self, relative):
        return self.sanitized / ACTIVE_SYMLINK / relative

    def test_publishes_a_complete_generation_behind_an_atomic_pointer(self):
        (self.incoming / "nested").mkdir()
        (self.incoming / "nested/source.txt").write_text("Hallo Max Mustermann", encoding="utf-8")

        changed, failed = self.publish()

        self.assertEqual((changed, failed), (1, 0))
        published = self.current_path("nested/source.txt")
        self.assertEqual(published.read_text(encoding="utf-8"), "Hallo [ICH]")
        # The active pointer resolves inside the generations directory.
        self.assertEqual(
            Path(os.readlink(self.sanitized / ACTIVE_SYMLINK)).parts[-2:],
            (GENERATIONS_DIRECTORY, active(self.sanitized).name),
        )

    def test_generation_records_inventory_and_redaction_fingerprint(self):
        (self.incoming / "notes.txt").write_text("Hallo Max Mustermann", encoding="utf-8")

        self.publish()

        generation = active(self.sanitized)
        metadata = json.loads(
            (generation / GENERATION_METADATA_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["generation"], generation.name)
        self.assertEqual(
            metadata["redaction_fingerprint"],
            anonymizer().fingerprint,
        )
        self.assertEqual(
            sorted(metadata["upstream_inventory"]),
            ["notes.txt"],
        )
        self.assertRegex(metadata["upstream_inventory"]["notes.txt"], r"^[0-9a-f]{64}$")

    def test_manifest_describes_stable_wiki_paths_and_current_source_paths(self):
        (self.incoming / "nested").mkdir()
        (self.incoming / "nested/source.txt").write_text("Hallo Max Mustermann", encoding="utf-8")

        self.publish()

        manifest = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"], "webdav")
        self.assertEqual(manifest["wiki_root"], "webdav")
        item = manifest["items"][0]
        self.assertEqual(item["source_key"], "nested/source.txt")
        self.assertEqual(item["source_path"], "current/nested/source.txt")
        self.assertEqual(item["wiki_path"], "webdav/nested/source.txt/index.md")
        self.assertEqual(item["claim"], {"source_path": "nested/source.txt"})
        self.assertEqual(
            item["frontmatter"],
            {
                "source_adapter": "webdav",
                "source_path": "nested/source.txt",
                "source_revision": item["source_revision"],
            },
        )

    def test_republishing_identical_content_keeps_revisions_and_pages_stable(self):
        (self.incoming / "notes.txt").write_text("Hallo Max Mustermann", encoding="utf-8")
        self.publish()
        first = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))

        changed, failed = self.publish()

        self.assertEqual((changed, failed), (0, 0))
        second = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(
            self.current_path("notes.txt").read_text(encoding="utf-8"),
            "Hallo [ICH]",
        )

    def test_upstream_deletions_are_published_only_with_the_new_generation(self):
        (self.incoming / "a.txt").write_text("eins", encoding="utf-8")
        (self.incoming / "b.txt").write_text("zwei", encoding="utf-8")
        self.publish()

        # The upstream file disappears, but the synchronization fails before
        # publication: the previous generation must stay active.
        (self.incoming / "b.txt").unlink()
        with mock.patch(
            "karpathy_wiki_ingest_webdav.sanitize_into_generation",
            side_effect=OSError("interrupted"),
        ):
            with self.assertRaises(OSError):
                self.publish()
        self.assertTrue(self.current_path("b.txt").is_file())

        changed, failed = self.publish()
        self.assertEqual((changed, failed), (0, 0))
        self.assertFalse(self.current_path("b.txt").exists())
        manifest = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["source_key"] for item in manifest["items"]], ["a.txt"])

    def test_failed_publication_rolls_back_to_the_previous_generation(self):
        (self.incoming / "notes.txt").write_text("eins", encoding="utf-8")
        self.publish()
        previous_generation = active(self.sanitized)
        (self.incoming / "notes.txt").write_text("zwei", encoding="utf-8")

        with mock.patch(
            "karpathy_wiki_ingest_webdav.write_manifest",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                self.publish()

        self.assertEqual(active(self.sanitized), previous_generation)
        self.assertEqual(self.current_path("notes.txt").read_text(encoding="utf-8"), "eins")
        # The unpublished generation and any staging leftovers are removed.
        self.assertEqual(
            [entry.name for entry in (self.sanitized / GENERATIONS_DIRECTORY).iterdir()],
            [previous_generation.name],
        )

    def test_abandoned_staging_directories_are_discarded(self):
        (self.incoming / "notes.txt").write_text("eins", encoding="utf-8")
        generations = self.sanitized / GENERATIONS_DIRECTORY
        generations.mkdir(parents=True)
        abandoned = generations / ".staging-abandoned"
        abandoned.mkdir()
        (abandoned / "leftover.txt").write_text("x", encoding="utf-8")

        self.publish()

        self.assertFalse(abandoned.exists())
        self.assertTrue(self.current_path("notes.txt").is_file())

    def test_retention_keeps_only_the_active_generation(self):
        (self.incoming / "notes.txt").write_text("eins", encoding="utf-8")
        self.publish()
        first = active(self.sanitized)
        (self.incoming / "notes.txt").write_text("zwei", encoding="utf-8")
        self.publish()

        self.assertFalse(first.exists())
        self.assertEqual(
            [entry.name for entry in (self.sanitized / GENERATIONS_DIRECTORY).iterdir()],
            [active(self.sanitized).name],
        )


class QuarantineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.incoming = self.root / "incoming"
        self.sanitized = self.root / "sanitized"
        self.quarantine = self.root / "quarantine"
        self.incoming.mkdir()

    def publish(self, anonymizer_instance=None):
        return publish_generation(
            self.incoming, self.sanitized, self.quarantine, anonymizer_instance or anonymizer()
        )

    def test_binary_files_are_quarantined_without_storing_source_content(self):
        (self.incoming / "nested").mkdir()
        (self.incoming / "nested/notes.txt").write_text("Hallo Max Mustermann", encoding="utf-8")
        (self.incoming / "private.pdf").write_bytes(b"%PDF\xff")

        changed, failed = self.publish()

        self.assertEqual((changed, failed), (1, 1))
        self.assertFalse((self.sanitized / ACTIVE_SYMLINK / "private.pdf").exists())
        generation = active(self.sanitized).name
        report = (self.quarantine / "private.pdf.error").read_text(encoding="utf-8")
        self.assertIn("path=private.pdf", report)
        self.assertIn("generation=" + generation, report)
        self.assertIn("error_type=UnicodeDecodeError", report)
        self.assertNotIn("%PDF", report)
        manifest = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["errors"],
            [{"path": "current/private.pdf", "error": "UnicodeDecodeError"}],
        )

    def test_privacy_validation_failure_keeps_the_file_out_of_the_generation(self):
        (self.incoming / "notes.txt").write_text("Hallo Max Mustermann", encoding="utf-8")
        # A residual literal raises PrivacyValidationError inside anonymize();
        # simulate it on the public shared class instead of a mock.
        real = TargetedAnonymizer.from_config(
            {
                "people": [
                    {
                        "replacement": "[ICH]",
                        "values": ["Max Mustermann"],
                    }
                ]
            }
        )
        with mock.patch.object(real, "anonymize", side_effect=ValueError("boom")):
            changed, failed = self.publish(real)

        self.assertEqual((changed, failed), (0, 1))
        self.assertFalse((self.sanitized / ACTIVE_SYMLINK / "notes.txt").exists())
        manifest = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["items"], [])
        self.assertEqual(
            manifest["errors"],
            [{"path": "current/notes.txt", "error": "ValueError"}],
        )

    def test_reserved_manifest_name_is_quarantined(self):
        (self.incoming / "manifest.json").write_text("upstream", encoding="utf-8")

        changed, failed = self.publish()

        self.assertEqual((changed, failed), (0, 1))
        report = (self.quarantine / "manifest.json.error").read_text(encoding="utf-8")
        self.assertIn("error_type=ReservedManifestName", report)
        # The provider manifest is the only manifest in the source directory;
        # the upstream file is never published under `current`.
        self.assertFalse((self.sanitized / ACTIVE_SYMLINK / "manifest.json").exists())
        manifest = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["items"], [])
        self.assertEqual(
            manifest["errors"],
            [{"path": "current/manifest.json", "error": "ReservedManifestName"}],
        )

    def test_reserved_generation_metadata_name_is_quarantined(self):
        (self.incoming / GENERATION_METADATA_FILENAME).write_text("upstream", encoding="utf-8")

        changed, failed = self.publish()

        self.assertEqual((changed, failed), (0, 1))
        report = (self.quarantine / f"{GENERATION_METADATA_FILENAME}.error").read_text(
            encoding="utf-8"
        )
        self.assertIn("error_type=ReservedGenerationMetadataName", report)
        # Only the plugin's own metadata is published under the reserved name.
        metadata = json.loads(
            (active(self.sanitized) / GENERATION_METADATA_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["generation"], active(self.sanitized).name)

    def test_reports_of_sources_that_are_gone_upstream_expire(self):
        (self.incoming / "notes.txt").write_text("eins", encoding="utf-8")
        self.publish()
        stale = self.quarantine / "gone.pdf.error"
        stale.write_text("path=gone.pdf\ngeneration=old\nerror_type=UnicodeDecodeError\n")

        self.publish()

        self.assertFalse(stale.exists())


class RedactionReloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.incoming = self.root / "incoming"
        self.sanitized = self.root / "sanitized"
        self.quarantine = self.root / "quarantine"
        self.redactions = self.root / "redactions.json"
        self.incoming.mkdir()

    def test_redaction_change_regenerates_published_files(self):
        (self.incoming / "notes.txt").write_text("Hallo Max Mustermann", encoding="utf-8")
        self.redactions.write_text(json.dumps(ANONYMIZER_CONFIGURATION), encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "WEBDAV_INCOMING_ROOT": str(self.incoming),
                "WEBDAV_SANITIZED_ROOT": str(self.sanitized),
                "WEBDAV_QUARANTINE_ROOT": str(self.quarantine),
                "WEBDAV_SYNC_INTERVAL": "1s",
                "WEBDAV_PATH": "Wiki Sources",
                "REDACTIONS_FILE": str(self.redactions),
                "HEALTH_PATH": str(self.root / "health.json"),
            },
            clear=True,
        ):
            with mock.patch("karpathy_wiki_ingest_webdav.synchronize"):
                run(once=True)
            self.assertEqual(
                (self.sanitized / ACTIVE_SYMLINK / "notes.txt").read_text(encoding="utf-8"),
                "Hallo [ICH]",
            )

            # Rotating the redactions file changes the fingerprint and forces
            # every applicable file to be regenerated on the next cycle.
            self.redactions.write_text(
                json.dumps({"people": [{"replacement": "[AUTOR]", "values": ["Max Mustermann"]}]}),
                encoding="utf-8",
            )
            with mock.patch("karpathy_wiki_ingest_webdav.synchronize"):
                run(once=True)

        published = (self.sanitized / ACTIVE_SYMLINK / "notes.txt").read_text(encoding="utf-8")
        self.assertEqual(published, "Hallo [AUTOR]")
        first = json.loads((self.root / "health.json").read_text())
        self.assertEqual(first["failed"], 0)

    def test_changed_redaction_file_counts_all_changed_files(self):
        (self.incoming / "notes.txt").write_text("Hallo Max Mustermann", encoding="utf-8")
        changed, failed = publish_generation(
            self.incoming,
            self.sanitized,
            self.quarantine,
            TargetedAnonymizer.from_config(ANONYMIZER_CONFIGURATION),
        )
        self.assertEqual((changed, failed), (1, 0))
        rotated, failed = publish_generation(
            self.incoming,
            self.sanitized,
            self.quarantine,
            TargetedAnonymizer.from_config(
                {"people": [{"replacement": "[AUTOR]", "values": ["Max Mustermann"]}]}
            ),
        )
        self.assertEqual((rotated, failed), (1, 0))
        self.assertEqual(
            (self.sanitized / ACTIVE_SYMLINK / "notes.txt").read_text(encoding="utf-8"),
            "Hallo [AUTOR]",
        )


class ConcurrentReadTests(unittest.TestCase):
    def test_concurrent_readers_never_observe_a_partial_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "incoming"
            sanitized = root / "sanitized"
            quarantine = root / "quarantine"
            incoming.mkdir()
            (incoming / "notes.txt").write_text("eins", encoding="utf-8")
            anonymizer_instance = anonymizer()
            publish_generation(incoming, sanitized, quarantine, anonymizer_instance)
            active = sanitized / ACTIVE_SYMLINK
            observed = set()
            stop = threading.Event()

            def reader():
                while not stop.is_set():
                    try:
                        observed.add((active / "notes.txt").read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        # A missing pointer or file is still a valid observation.
                        pass

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            try:
                for content in ("zwei", "drei"):
                    (incoming / "notes.txt").write_text(content, encoding="utf-8")
                    publish_generation(incoming, sanitized, quarantine, anonymizer_instance)
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertTrue(observed <= {"eins", "zwei", "drei"}, observed)
            self.assertTrue(observed, "the reader must have observed publications")


class IntervalTests(unittest.TestCase):
    def test_parses_plain_seconds_and_durations(self):
        self.assertEqual(interval_seconds("90"), 90)
        self.assertEqual(interval_seconds("45s"), 45)
        self.assertEqual(interval_seconds("15m"), 900)
        self.assertEqual(interval_seconds("1h"), 3600)
        self.assertEqual(interval_seconds("1.5m"), 90)
        self.assertEqual(interval_seconds("0.5h"), 1800)

    def test_rejects_non_positive_and_unparseable_values(self):
        for value in ("0", "-5m", "0s", "", "abc", "15x", "0.5", "0.5s", "90.5", "1.5s"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    interval_seconds(value)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_environment_overrides(self):
        with mock.patch.dict(os.environ, {"REDACTIONS_FILE": "/redactions.json"}, clear=True):
            environment = Settings.from_env()
        self.assertEqual(environment.incoming, Path("/data/incoming/webdav"))
        self.assertEqual(environment.sanitized, Path("/data/sanitized/webdav"))
        self.assertEqual(environment.quarantine, Path("/data/quarantine/webdav"))
        self.assertEqual(environment.interval, 900)
        self.assertEqual(environment.redactions, Path("/redactions.json"))
        self.assertEqual(environment.health_path, Path("/tmp/health.json"))

    def test_environment_overrides(self):
        environment_variables = {
            "WEBDAV_INCOMING_ROOT": "/i",
            "WEBDAV_SANITIZED_ROOT": "/s",
            "WEBDAV_QUARANTINE_ROOT": "/q",
            "WEBDAV_SYNC_INTERVAL": "2m",
            "REDACTIONS_FILE": "/r.json",
            "HEALTH_PATH": "/h.json",
        }
        with mock.patch.dict(os.environ, environment_variables, clear=True):
            environment = Settings.from_env()
        self.assertEqual(
            environment,
            Settings(
                incoming=Path("/i"),
                sanitized=Path("/s"),
                quarantine=Path("/q"),
                interval=120,
                redactions=Path("/r.json"),
                health_path=Path("/h.json"),
            ),
        )

    def test_missing_redactions_file_is_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "REDACTIONS_FILE is required"):
                Settings.from_env()


class SynchronizeTests(unittest.TestCase):
    def test_rclone_command_contains_retries(self):
        with mock.patch("karpathy_wiki_ingest_webdav.subprocess.run") as run_mock:
            synchronize(Path("/incoming"), "Wiki Sources")
        command = run_mock.call_args.args[0]
        self.assertEqual(command[0], "rclone")
        self.assertIn("webdav:Wiki Sources", command)
        self.assertIn(str(Path("/incoming")), command)
        self.assertIn("--create-empty-src-dirs", command)
        self.assertEqual(command[command.index("--retries") + 1], "3")
        self.assertEqual(command[command.index("--low-level-retries") + 1], "10")

    def test_rclone_failure_raises(self):
        with mock.patch(
            "karpathy_wiki_ingest_webdav.subprocess.run",
            side_effect=subprocess.CalledProcessError(5, "rclone"),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                synchronize(Path("/incoming"), "Wiki Sources")


class RunLoopTests(unittest.TestCase):
    def environment(self, root, **overrides):
        variables = {
            "WEBDAV_PATH": "Wiki Sources",
            "WEBDAV_INCOMING_ROOT": str(root / "incoming"),
            "WEBDAV_SANITIZED_ROOT": str(root / "sanitized"),
            "WEBDAV_QUARANTINE_ROOT": str(root / "quarantine"),
            "WEBDAV_SYNC_INTERVAL": "1s",
            "REDACTIONS_FILE": str(root / "redactions.json"),
            "HEALTH_PATH": str(root / "health.json"),
        }
        variables.update(overrides)
        return variables

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "redactions.json").write_text(
            json.dumps(ANONYMIZER_CONFIGURATION), encoding="utf-8"
        )

    def test_once_succeeds_without_changes(self):
        with (
            mock.patch.dict(os.environ, self.environment(self.root), clear=True),
            mock.patch("karpathy_wiki_ingest_webdav.synchronize"),
        ):
            run(once=True)
        health = json.loads((self.root / "health.json").read_text())
        self.assertEqual(health["failed"], 0)

    def test_once_reports_rclone_failure_as_exit_code(self):
        error = subprocess.CalledProcessError(5, "rclone")
        with (
            mock.patch.dict(os.environ, self.environment(self.root), clear=True),
            mock.patch(
                "karpathy_wiki_ingest_webdav.synchronize",
                side_effect=error,
            ),
        ):
            with self.assertRaises(SystemExit) as raised:
                run(once=True)
        self.assertEqual(raised.exception.code, 1)
        health = json.loads((self.root / "health.json").read_text())
        self.assertEqual(health["failed"], 1)
        # The failed cycle must not publish anything.
        self.assertFalse((self.root / "sanitized" / "manifest.json").exists())

    def test_daemon_retries_after_rclone_failure_and_writes_health(self):
        stop_event = threading.Event()
        attempts = []

        def first_failure_then_success(*_args):
            attempts.append(True)
            if len(attempts) == 1:
                # The first cycle fails; the daemon must retry on the
                # next interval instead of stopping.
                raise subprocess.CalledProcessError(5, "rclone")
            # The retry succeeded; shut the daemon down afterwards.
            stop_event.set()

        with (
            mock.patch.dict(os.environ, self.environment(self.root), clear=True),
            mock.patch(
                "karpathy_wiki_ingest_webdav.install_stop_handler",
                return_value=stop_event,
            ),
            mock.patch(
                "karpathy_wiki_ingest_webdav.synchronize",
                side_effect=first_failure_then_success,
            ),
            mock.patch(
                "karpathy_wiki_ingest_webdav.publish_generation",
                return_value=(0, 0),
            ) as publish,
        ):
            run(once=False)
        self.assertEqual(len(attempts), 2)
        publish.assert_called_once()
        health = json.loads((self.root / "health.json").read_text())
        self.assertEqual(health["failed"], 0)

    def test_redaction_configuration_is_reloaded_for_every_cycle(self):
        stop_event = threading.Event()
        loads = []

        class ReloadTrackingAnonymizer(TargetedAnonymizer):
            pass

        def from_file(path):
            loads.append(path)
            if len(loads) == 1:
                return ReloadTrackingAnonymizer.from_config(ANONYMIZER_CONFIGURATION)
            stop_event.set()
            return ReloadTrackingAnonymizer.from_config(ANONYMIZER_CONFIGURATION)

        with (
            mock.patch.dict(os.environ, self.environment(self.root), clear=True),
            mock.patch(
                "karpathy_wiki_ingest_webdav.install_stop_handler",
                return_value=stop_event,
            ),
            mock.patch(
                "karpathy_wiki_ingest_webdav.TargetedAnonymizer.from_file",
                side_effect=from_file,
            ),
            mock.patch("karpathy_wiki_ingest_webdav.synchronize"),
            mock.patch(
                "karpathy_wiki_ingest_webdav.publish_generation",
                return_value=(0, 0),
            ),
        ):
            run(once=False)
        self.assertEqual(len(loads), 2)

    def test_stop_event_interrupts_interval_wait(self):
        stop_event = threading.Event()

        def one_successful_cycle(*_args):
            stop_event.set()

        with (
            mock.patch.dict(os.environ, self.environment(self.root), clear=True),
            mock.patch(
                "karpathy_wiki_ingest_webdav.install_stop_handler",
                return_value=stop_event,
            ),
            mock.patch(
                "karpathy_wiki_ingest_webdav.synchronize",
                side_effect=one_successful_cycle,
            ) as synchronize_mock,
        ):
            run(once=False)
        synchronize_mock.assert_called_once()
        self.assertEqual(json.loads((self.root / "health.json").read_text())["failed"], 0)

    def test_missing_webdav_path_configuration_fails_without_crash(self):
        variables = self.environment(self.root)
        variables.pop("WEBDAV_PATH", None)
        with mock.patch.dict(os.environ, variables, clear=True):
            with self.assertRaises(SystemExit) as raised:
                run(once=True)
        self.assertEqual(raised.exception.code, 1)
        health = json.loads((self.root / "health.json").read_text())
        self.assertEqual(health["failed"], 1)


class StopHandlerTests(unittest.TestCase):
    def test_sigterm_sets_stop_event(self):
        stop_event = install_stop_handler()
        os.kill(os.getpid(), __import__("signal").SIGTERM)
        self.assertTrue(stop_event.wait(1))


class HealthcheckModuleTests(unittest.TestCase):
    def test_webdav_interval_is_parsed_from_duration(self):
        with mock.patch.dict(
            os.environ,
            {"WEBDAV_SYNC_INTERVAL": "15m"},
            clear=True,
        ):
            self.assertEqual(healthcheck.interval(), 900)

    def test_paperless_interval_takes_precedence(self):
        with mock.patch.dict(
            os.environ,
            {"SYNC_INTERVAL_SECONDS": "300", "WEBDAV_SYNC_INTERVAL": "15m"},
            clear=True,
        ):
            self.assertEqual(healthcheck.interval(), 300)


if __name__ == "__main__":
    unittest.main()
