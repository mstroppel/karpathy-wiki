import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from karpathy_wiki_ingest import health as healthcheck
from karpathy_wiki_ingest.shared import TargetedAnonymizer
from karpathy_wiki_ingest.state import (
    JOB_DEAD,
    JOB_LEASED,
    JOB_PENDING,
    JOB_SUCCEEDED,
    JOB_SUPERSEDED,
    StateError,
    StateStore,
)
from karpathy_wiki_ingest_webdav import (
    ACTIVE_SYMLINK,
    GENERATION_METADATA_FILENAME,
    GENERATIONS_DIRECTORY,
    Settings,
    active_generation,
    cycle_idempotency_key,
    install_stop_handler,
    interval_seconds,
    process_cycle,
    publish_generation,
    publish_with_heartbeat,
    published_matches,
    record_generation,
    run,
    synchronize,
    upstream_inventory,
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
        (self.incoming / "nested/source.md").write_text("Hallo Max Mustermann", encoding="utf-8")

        changed, failed = self.publish()

        self.assertEqual((changed, failed), (1, 0))
        published = self.current_path("nested/source.md")
        self.assertEqual(published.read_text(encoding="utf-8"), "Hallo [ICH]")
        # The active pointer resolves inside the generations directory.
        self.assertEqual(
            Path(os.readlink(self.sanitized / ACTIVE_SYMLINK)).parts[-2:],
            (GENERATIONS_DIRECTORY, active(self.sanitized).name),
        )

    def test_generation_records_inventory_and_redaction_fingerprint(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")

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
            ["notes.md"],
        )
        self.assertRegex(metadata["upstream_inventory"]["notes.md"], r"^[0-9a-f]{64}$")

    def test_non_markdown_files_are_ignored(self):
        (self.incoming / "notes.md").write_text("safe", encoding="utf-8")
        (self.incoming / "attachment.txt").write_text("ignored", encoding="utf-8")

        changed, failed = self.publish()

        self.assertEqual((changed, failed), (1, 0))
        self.assertTrue(self.current_path("notes.md").is_file())
        self.assertFalse(self.current_path("attachment.txt").exists())
        self.assertFalse((self.quarantine / "attachment.txt.error").exists())

    def test_manifest_describes_stable_wiki_paths_and_immutable_source_paths(self):
        (self.incoming / "nested").mkdir()
        (self.incoming / "nested/source.md").write_text("Hallo Max Mustermann", encoding="utf-8")

        self.publish()

        manifest = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"], "webdav")
        self.assertEqual(manifest["wiki_root"], "webdav")
        item = manifest["items"][0]
        self.assertEqual(item["source_key"], "nested/source.md")
        self.assertEqual(
            item["source_path"],
            f"{GENERATIONS_DIRECTORY}/{active(self.sanitized).name}/nested/source.md",
        )
        self.assertEqual(item["wiki_path"], "webdav/nested/source.md/index.md")
        self.assertEqual(item["claim"], {"source_path": "nested/source.md"})
        self.assertEqual(
            item["frontmatter"],
            {
                "source_adapter": "webdav",
                "source_path": "nested/source.md",
                "source_revision": item["source_revision"],
            },
        )

    def test_republishing_identical_content_keeps_revisions_and_pages_stable(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        self.publish()
        first = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))

        changed, failed = self.publish()

        self.assertEqual((changed, failed), (0, 0))
        second = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [
                {key: value for key, value in item.items() if key != "source_path"}
                for item in first["items"]
            ],
            [
                {key: value for key, value in item.items() if key != "source_path"}
                for item in second["items"]
            ],
        )
        self.assertEqual(
            self.current_path("notes.md").read_text(encoding="utf-8"),
            "Hallo [ICH]",
        )

    def test_upstream_deletions_are_published_only_with_the_new_generation(self):
        (self.incoming / "a.md").write_text("eins", encoding="utf-8")
        (self.incoming / "b.md").write_text("zwei", encoding="utf-8")
        self.publish()

        # The upstream file disappears, but the synchronization fails before
        # publication: the previous generation must stay active.
        (self.incoming / "b.md").unlink()
        with mock.patch(
            "karpathy_wiki_ingest_webdav.sanitize_into_generation",
            side_effect=OSError("interrupted"),
        ):
            with self.assertRaises(OSError):
                self.publish()
        self.assertTrue(self.current_path("b.md").is_file())

        changed, failed = self.publish()
        self.assertEqual((changed, failed), (0, 0))
        self.assertFalse(self.current_path("b.md").exists())
        manifest = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["source_key"] for item in manifest["items"]], ["a.md"])

    def test_failed_publication_rolls_back_to_the_previous_generation(self):
        (self.incoming / "notes.md").write_text("eins", encoding="utf-8")
        self.publish()
        previous_generation = active(self.sanitized)
        (self.incoming / "notes.md").write_text("zwei", encoding="utf-8")

        with mock.patch(
            "karpathy_wiki_ingest_webdav.write_manifest",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                self.publish()

        self.assertEqual(active(self.sanitized), previous_generation)
        self.assertEqual(self.current_path("notes.md").read_text(encoding="utf-8"), "eins")
        # The unpublished generation and any staging leftovers are removed.
        self.assertEqual(
            [entry.name for entry in (self.sanitized / GENERATIONS_DIRECTORY).iterdir()],
            [previous_generation.name],
        )

    def test_abandoned_staging_directories_are_discarded(self):
        (self.incoming / "notes.md").write_text("eins", encoding="utf-8")
        generations = self.sanitized / GENERATIONS_DIRECTORY
        generations.mkdir(parents=True)
        abandoned = generations / ".staging-abandoned"
        abandoned.mkdir()
        (abandoned / "leftover.md").write_text("x", encoding="utf-8")

        self.publish()

        self.assertFalse(abandoned.exists())
        self.assertTrue(self.current_path("notes.md").is_file())

    def test_retention_keeps_only_the_active_generation(self):
        (self.incoming / "notes.md").write_text("eins", encoding="utf-8")
        self.publish()
        first = active(self.sanitized)
        (self.incoming / "notes.md").write_text("zwei", encoding="utf-8")
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

    def current_path(self, relative):
        return self.sanitized / ACTIVE_SYMLINK / relative

    def test_binary_files_are_quarantined_without_storing_source_content(self):
        (self.incoming / "nested").mkdir()
        (self.incoming / "nested/notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        (self.incoming / "private.md").write_bytes(b"%PDF\xff")

        changed, failed = self.publish()

        self.assertEqual((changed, failed), (0, 1))
        self.assertFalse((self.sanitized / ACTIVE_SYMLINK / "private.md").exists())
        report = (self.quarantine / "private.md.error").read_text(encoding="utf-8")
        self.assertIn("path=private.md", report)
        self.assertIn("generation=", report)
        self.assertIn("error_type=UnicodeDecodeError", report)
        self.assertNotIn("%PDF", report)
        self.assertFalse((self.sanitized / "manifest.json").exists())

    def test_sanitization_failure_keeps_the_previous_generation(self):
        (self.incoming / "notes.md").write_text("safe", encoding="utf-8")
        self.publish()
        previous = active(self.sanitized)
        previous_manifest = (self.sanitized / "manifest.json").read_text(encoding="utf-8")

        (self.incoming / "broken.md").write_bytes(b"\xff")
        changed, failed = self.publish()

        self.assertEqual((changed, failed), (0, 1))
        self.assertEqual(active(self.sanitized), previous)
        self.assertEqual(
            (self.sanitized / "manifest.json").read_text(encoding="utf-8"),
            previous_manifest,
        )
        self.assertEqual(self.current_path("notes.md").read_text(encoding="utf-8"), "safe")
        self.assertFalse(self.current_path("broken.md").exists())

    def test_privacy_validation_failure_keeps_the_file_out_of_the_generation(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
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
        self.assertFalse((self.sanitized / ACTIVE_SYMLINK / "notes.md").exists())
        self.assertFalse((self.sanitized / "manifest.json").exists())

    def test_reports_of_sources_that_are_gone_upstream_expire(self):
        (self.incoming / "notes.md").write_text("eins", encoding="utf-8")
        self.publish()
        stale = self.quarantine / "gone.md.error"
        stale.write_text("path=gone.md\ngeneration=old\nerror_type=UnicodeDecodeError\n")

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
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
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
                (self.sanitized / ACTIVE_SYMLINK / "notes.md").read_text(encoding="utf-8"),
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

        published = (self.sanitized / ACTIVE_SYMLINK / "notes.md").read_text(encoding="utf-8")
        self.assertEqual(published, "Hallo [AUTOR]")
        first = json.loads((self.root / "health.json").read_text())
        self.assertEqual(first["failed"], 0)

    def test_changed_redaction_file_counts_all_changed_files(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
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
            (self.sanitized / ACTIVE_SYMLINK / "notes.md").read_text(encoding="utf-8"),
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
            (incoming / "notes.md").write_text("eins", encoding="utf-8")
            anonymizer_instance = anonymizer()
            publish_generation(incoming, sanitized, quarantine, anonymizer_instance)
            active = sanitized / ACTIVE_SYMLINK
            observed = set()
            stop = threading.Event()

            def reader():
                while not stop.is_set():
                    try:
                        observed.add((active / "notes.md").read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        # A missing pointer or file is still a valid observation.
                        pass

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            try:
                for content in ("zwei", "drei"):
                    (incoming / "notes.md").write_text(content, encoding="utf-8")
                    publish_generation(incoming, sanitized, quarantine, anonymizer_instance)
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertTrue(observed <= {"eins", "zwei", "drei"}, observed)
            self.assertTrue(observed, "the reader must have observed publications")


class DurableStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.incoming = self.root / "incoming"
        self.sanitized = self.root / "sanitized"
        self.quarantine = self.root / "quarantine"
        self.state_path = self.root / "ingest.sqlite3"
        self.incoming.mkdir()

    def store(self) -> StateStore:
        return StateStore.open(self.state_path)

    def cycle(
        self, instance: TargetedAnonymizer, store: StateStore | None, now: int
    ) -> tuple[int, int, bool]:
        return process_cycle(
            self.incoming, self.sanitized, self.quarantine, instance, store, interval=60, now=now
        )

    def test_cycle_key_is_stable_for_identical_content_and_redactions(self):
        first = cycle_idempotency_key("fp", {"a.md": "1" * 64})
        second = cycle_idempotency_key("fp", {"a.md": "1" * 64})
        changed = cycle_idempotency_key("fp", {"a.md": "2" * 64})
        rotated = cycle_idempotency_key("other", {"a.md": "1" * 64})
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertNotEqual(first, rotated)
        self.assertTrue(first.startswith("webdav-generation:"))

    def test_published_matches_detects_the_published_cycle(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        instance = anonymizer()
        publish_generation(self.incoming, self.sanitized, self.quarantine, instance)
        inventory = upstream_inventory(self.incoming)
        self.assertTrue(published_matches(self.sanitized, instance, inventory))
        self.assertFalse(published_matches(self.sanitized, instance, {"notes.md": "1" * 64}))
        rotated = TargetedAnonymizer.from_config(
            {"people": [{"replacement": "[AUTOR]", "values": ["Max Mustermann"]}]}
        )
        self.assertFalse(published_matches(self.sanitized, rotated, inventory))
        self.assertFalse(published_matches(self.root / "missing", instance, inventory))

    def test_record_generation_records_manifest_identity_once(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        instance = anonymizer()
        publish_generation(self.incoming, self.sanitized, self.quarantine, instance)
        generation = active_generation(self.sanitized)
        assert generation is not None
        store = self.store()
        try:
            record_generation(store, self.sanitized, generation, instance.fingerprint, now=100)
            # Recording the same generation again is a no-op.
            record_generation(store, self.sanitized, generation, instance.fingerprint, now=100)
            metrics = store.metrics()
        finally:
            store.close()
        self.assertEqual(metrics["source_generations"], 1)

    def test_durable_cycle_skips_unchanged_upstream(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        instance = anonymizer()
        store = self.store()
        try:
            changed, failed, degraded = self.cycle(instance, store, now=1000)
            self.assertEqual((changed, failed, degraded), (1, 0, False))
            generation = active_generation(self.sanitized)
            assert generation is not None
            key = cycle_idempotency_key(instance.fingerprint, upstream_inventory(self.incoming))
            job = store.job_by_idempotency_key(key)
            assert job is not None
            self.assertEqual(job.state, JOB_SUCCEEDED)
            self.assertEqual(job.result, {"generation": generation.name, "changed": 1})

            # An identical cycle re-submits the same accepted work and keeps
            # the active generation instead of republishing it.
            changed, failed, degraded = self.cycle(instance, store, now=1120)
            self.assertEqual((changed, failed, degraded), (0, 0, False))
            self.assertEqual(active_generation(self.sanitized), generation)
        finally:
            store.close()

    def test_durable_cycle_records_retries_and_dead_jobs(self):
        (self.incoming / "broken.md").write_bytes(b"\xff")
        instance = anonymizer()
        store = self.store()
        try:
            # Every cycle fails identically; the job backs off and eventually
            # becomes dead instead of retrying forever.
            for attempt in range(5):
                changed, failed, degraded = self.cycle(instance, store, now=1000 + attempt * 600)
                self.assertEqual((changed, failed, degraded), (0, 1, False))
            key = cycle_idempotency_key(instance.fingerprint, upstream_inventory(self.incoming))
            job = store.job_by_idempotency_key(key)
            assert job is not None
            self.assertEqual(job.state, JOB_DEAD)
            self.assertEqual(job.attempts, 5)
            self.assertEqual(job.last_error, "quarantined:1")

            # A dead job with unchanged content stays dead and keeps the
            # failure visible through the returned failed count.
            changed, failed, degraded = self.cycle(instance, store, now=100000)
            self.assertEqual((changed, failed, degraded), (0, 1, False))
            self.assertIsNone(active_generation(self.sanitized))
        finally:
            store.close()

    def test_interrupted_cycle_is_recovered_without_republishing(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        instance = anonymizer()
        store = self.store()
        try:
            key = cycle_idempotency_key(instance.fingerprint, upstream_inventory(self.incoming))
            # Simulate a crash after publication but before completion.
            publish_generation(self.incoming, self.sanitized, self.quarantine, instance)
            generation = active_generation(self.sanitized)
            assert generation is not None
            job, _ = store.enqueue("ingest", {"provider": "webdav"}, idempotency_key=key, now=1000)
            lease = store.claim(job.id, "interrupted", lease_seconds=60, now=1000)
            assert lease is not None

            # The expired lease is recovered and the published generation is
            # completed without a second publication.
            changed, failed, degraded = self.cycle(instance, store, now=1100)
            self.assertEqual((changed, failed, degraded), (0, 0, False))
            self.assertEqual(active_generation(self.sanitized), generation)
            recovered = store.job(job.id)
            self.assertEqual(recovered.state, JOB_SUCCEEDED)
            events = [event["to_state"] for event in store.job_events(job.id)]
            self.assertEqual(
                events,
                [JOB_PENDING, JOB_LEASED, JOB_PENDING, JOB_LEASED, JOB_SUCCEEDED],
            )
        finally:
            store.close()

    def test_losing_the_published_generation_arms_the_job_again(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        instance = anonymizer()
        store = self.store()
        try:
            changed, failed, degraded = self.cycle(instance, store, now=1000)
            self.assertEqual((changed, failed, degraded), (1, 0, False))
            first = active_generation(self.sanitized)
            assert first is not None

            # The publication disappears, for example after a manual recovery:
            # the succeeded job is rearmed and republished.
            shutil.rmtree(self.sanitized)
            changed, failed, degraded = self.cycle(instance, store, now=1200)
            self.assertEqual((changed, failed, degraded), (1, 0, False))
            self.assertIsNotNone(active_generation(self.sanitized))
            self.assertNotEqual(active_generation(self.sanitized), first)
        finally:
            store.close()

    def test_an_unavailable_store_degrades_to_stateless_publishing(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        instance = anonymizer()
        store = self.store()
        with mock.patch.object(
            store, "enqueue", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            changed, failed, degraded = self.cycle(instance, store, now=1000)
        store.close()
        self.assertEqual((changed, failed, degraded), (1, 0, True))
        self.assertIsNotNone(active_generation(self.sanitized))

    def test_publication_renews_the_lease_while_the_work_runs(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        instance = anonymizer()
        store = self.store()
        try:
            key = cycle_idempotency_key(instance.fingerprint, upstream_inventory(self.incoming))
            job, _ = store.enqueue("ingest", {"provider": "webdav"}, idempotency_key=key, now=0)
            lease = store.claim(job.id, "worker", lease_seconds=2, now=int(time.time()))
            assert lease is not None

            def slow_work(guard: Callable[[], None]) -> tuple[int, int]:
                # Longer than the lease: without renewal the lease would expire
                # in the middle of the work.
                time.sleep(3)
                return 1, 0

            changed, failed = publish_with_heartbeat(slow_work, store, lease.id, 2)
            self.assertEqual((changed, failed), (1, 0))
            # The lease was renewed while the work ran and is still held.
            renewed = store.lease(lease.id)
            self.assertGreater(renewed.expires_at, lease.expires_at)
        finally:
            store.close()

    def test_run_recovers_a_failed_state_store(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        redactions = self.root / "redactions.json"
        redactions.write_text(json.dumps(ANONYMIZER_CONFIGURATION), encoding="utf-8")
        broken_path = self.root / "state-directory"
        broken_path.mkdir()
        base_environment = {
            "WEBDAV_INCOMING_ROOT": str(self.incoming),
            "WEBDAV_SANITIZED_ROOT": str(self.sanitized),
            "WEBDAV_QUARANTINE_ROOT": str(self.quarantine),
            "WEBDAV_SYNC_INTERVAL": "1s",
            "WEBDAV_PATH": "Wiki Sources",
            "REDACTIONS_FILE": str(redactions),
            "HEALTH_PATH": str(self.root / "health.json"),
        }
        with (
            mock.patch.dict(
                os.environ, {**base_environment, "INGEST_STATE_PATH": str(broken_path)}, clear=True
            ),
            mock.patch("karpathy_wiki_ingest_webdav.synchronize"),
        ):
            # A state path that cannot be opened degrades the cycle and keeps
            # the daemon unhealthy instead of reporting false success.
            with self.assertRaises(SystemExit):
                run(once=True)
            first = json.loads((self.root / "health.json").read_text())
            self.assertEqual(first["failed"], 1)
            self.assertIsNotNone(active_generation(self.sanitized))

            # Once the store is available again the daemon recovers and
            # reports healthy coordinated work.
            with mock.patch.dict(
                os.environ,
                {**base_environment, "INGEST_STATE_PATH": str(self.state_path)},
                clear=True,
            ):
                run(once=True)
        record = json.loads((self.root / "health.json").read_text())
        self.assertEqual(record["failed"], 0)
        self.assertEqual(record["metrics"]["jobs"][JOB_SUCCEEDED], 1)

    def test_superseded_inputs_do_not_stay_pending_forever(self):
        instance = anonymizer()
        store = self.store()
        try:
            # An accepted job whose upstream inventory is later replaced by a
            # new synchronization can never run again; it becomes superseded.
            store.enqueue(
                "ingest",
                {"provider": "webdav", "upstream_files": 2},
                idempotency_key="old",
                now=100,
            )
            (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
            changed, failed, degraded = self.cycle(instance, store, now=1000)
            self.assertEqual((changed, failed, degraded), (1, 0, False))
            superseded = store.job_by_idempotency_key("old")
            assert superseded is not None
            self.assertEqual(superseded.state, JOB_SUPERSEDED)
        finally:
            store.close()

    def test_a_stale_manifest_forces_republication(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        instance = anonymizer()
        store = self.store()
        try:
            changed, failed, degraded = self.cycle(instance, store, now=1000)
            self.assertEqual((changed, failed, degraded), (1, 0, False))
            generation = active_generation(self.sanitized)
            assert generation is not None

            # A crash between the pointer switch and the manifest write leaves
            # a matching generation with a stale manifest: the cycle must not
            # treat that state as published.
            (self.sanitized / "manifest.json").write_text('{"contract": "bogus"}', encoding="utf-8")
            changed, failed, degraded = self.cycle(instance, store, now=1100)
            self.assertEqual((changed, failed, degraded), (1, 0, False))
            self.assertNotEqual(active_generation(self.sanitized), generation)
            republished = active_generation(self.sanitized)
            assert republished is not None
            manifest = json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn(
                f"{GENERATIONS_DIRECTORY}/{republished.name}/",
                manifest["items"][0]["source_path"],
            )
        finally:
            store.close()

    def test_empty_old_manifest_does_not_complete_a_new_generation(self):
        instance = anonymizer()
        store = self.store()
        try:
            publish_generation(self.incoming, self.sanitized, self.quarantine, instance)
            (self.incoming / "notes.md").write_text("new", encoding="utf-8")
            inventory = upstream_inventory(self.incoming)
            key = cycle_idempotency_key(instance.fingerprint, inventory)
            job, _ = store.enqueue("ingest", {"provider": "webdav"}, idempotency_key=key, now=1000)
            lease = store.claim(job.id, "interrupted", lease_seconds=60, now=1000)
            assert lease is not None
            old_manifest = (self.sanitized / "manifest.json").read_bytes()
            publish_generation(self.incoming, self.sanitized, self.quarantine, instance)
            (self.sanitized / "manifest.json").write_bytes(old_manifest)
            interrupted = active(self.sanitized)

            self.assertFalse(published_matches(self.sanitized, instance, inventory))
            self.assertEqual(self.cycle(instance, store, now=1100), (1, 0, False))
            self.assertNotEqual(active(self.sanitized), interrupted)
            manifest = json.loads((self.sanitized / "manifest.json").read_text())
            self.assertEqual([item["source_key"] for item in manifest["items"]], ["notes.md"])
            self.assertEqual(store.job(job.id).state, JOB_SUCCEEDED)
        finally:
            store.close()

    def test_publication_exceptions_are_recorded_and_bounded(self):
        (self.incoming / "notes.md").write_text("new", encoding="utf-8")
        instance = anonymizer()
        store = self.store()
        try:
            key = cycle_idempotency_key(instance.fingerprint, upstream_inventory(self.incoming))
            with mock.patch(
                "karpathy_wiki_ingest_webdav.write_manifest", side_effect=OSError("private data")
            ):
                for attempt in range(5):
                    with self.assertRaises(OSError):
                        self.cycle(instance, store, now=1000 + attempt * 600)
                    job = store.job_by_idempotency_key(key)
                    assert job is not None
                    self.assertEqual(job.last_error, "publication:OSError")
                    self.assertEqual(job.attempts, attempt + 1)
                    self.assertEqual(job.state, JOB_DEAD if attempt == 4 else JOB_PENDING)
                self.assertEqual(self.cycle(instance, store, now=5000), (0, 1, False))
            self.assertIsNone(active_generation(self.sanitized))
        finally:
            store.close()

    def test_removed_lease_fences_and_joins_publication_worker(self):
        (self.incoming / "notes.md").write_text("new", encoding="utf-8")
        instance = anonymizer()
        store = self.store()
        try:
            job, _ = store.enqueue("ingest", {"provider": "webdav"}, idempotency_key="lost")
            lease = store.claim(job.id, "worker", lease_seconds=2)
            assert lease is not None
            started = threading.Event()
            release = threading.Event()

            def work(guard: Callable[[], None]) -> tuple[int, int]:
                started.set()
                release.wait(timeout=5)
                return publish_generation(
                    self.incoming, self.sanitized, self.quarantine, instance, commit_guard=guard
                )

            def recover() -> None:
                other = self.store()
                try:
                    self.assertTrue(started.wait(timeout=5))
                    other.expire_leases(now=lease.expires_at)
                    # Let the renewal loop observe the missing lease and fence
                    # the worker before it reaches the pointer switch.
                    time.sleep(1.3)
                finally:
                    other.close()
                    release.set()

            recovery = threading.Thread(target=recover)
            recovery.start()
            try:
                with self.assertRaises(StateError):
                    publish_with_heartbeat(work, store, lease.id, 2)
            finally:
                release.set()
                recovery.join(timeout=5)
            self.assertIsNone(active_generation(self.sanitized))
        finally:
            store.close()

    def test_a_lost_lease_fences_the_publication(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        instance = anonymizer()
        store = self.store()
        try:
            key = cycle_idempotency_key(instance.fingerprint, upstream_inventory(self.incoming))
            job, _ = store.enqueue("ingest", {"provider": "webdav"}, idempotency_key=key, now=0)
            lease = store.claim(job.id, "worker", lease_seconds=2, now=int(time.time()))
            assert lease is not None

            def fenced_work(guard: Callable[[], None]) -> tuple[int, int]:
                # Renewal fails while the work runs; the commit guard must
                # abort it before the pointer switch.
                time.sleep(3)
                return publish_generation(
                    self.incoming,
                    self.sanitized,
                    self.quarantine,
                    instance,
                    commit_guard=guard,
                )

            with (
                mock.patch.object(store, "heartbeat", side_effect=StateError("store gone")),
                self.assertRaises(StateError),
            ):
                publish_with_heartbeat(fenced_work, store, lease.id, 2)
            self.assertIsNone(active_generation(self.sanitized))
        finally:
            store.close()

    def test_run_records_state_and_health_metrics(self):
        (self.incoming / "notes.md").write_text("Hallo Max Mustermann", encoding="utf-8")
        redactions = self.root / "redactions.json"
        redactions.write_text(json.dumps(ANONYMIZER_CONFIGURATION), encoding="utf-8")
        environment_variables = {
            "WEBDAV_INCOMING_ROOT": str(self.incoming),
            "WEBDAV_SANITIZED_ROOT": str(self.sanitized),
            "WEBDAV_QUARANTINE_ROOT": str(self.quarantine),
            "WEBDAV_SYNC_INTERVAL": "1s",
            "WEBDAV_PATH": "Wiki Sources",
            "REDACTIONS_FILE": str(redactions),
            "HEALTH_PATH": str(self.root / "health.json"),
            "INGEST_STATE_PATH": str(self.state_path),
        }
        with (
            mock.patch.dict(os.environ, environment_variables, clear=True),
            mock.patch("karpathy_wiki_ingest_webdav.synchronize"),
        ):
            run(once=True)
            run(once=True)
        store = self.store()
        try:
            metrics = store.metrics()
            self.assertEqual(metrics["jobs"][JOB_SUCCEEDED], 1)
            self.assertEqual(metrics["source_generations"], 1)
        finally:
            store.close()
        record = json.loads((self.root / "health.json").read_text())
        self.assertEqual(record["failed"], 0)
        self.assertEqual(record["metrics"]["jobs"][JOB_SUCCEEDED], 1)


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
        self.assertIsNone(environment.state_path)

    def test_environment_overrides(self):
        environment_variables = {
            "WEBDAV_INCOMING_ROOT": "/i",
            "WEBDAV_SANITIZED_ROOT": "/s",
            "WEBDAV_QUARANTINE_ROOT": "/q",
            "WEBDAV_SYNC_INTERVAL": "2m",
            "REDACTIONS_FILE": "/r.json",
            "HEALTH_PATH": "/h.json",
            "INGEST_STATE_PATH": "/state/ingest.sqlite3",
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
                state_path=Path("/state/ingest.sqlite3"),
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
