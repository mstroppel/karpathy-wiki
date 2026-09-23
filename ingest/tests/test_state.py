"""Tests for the durable SQLite state store: jobs, leases, generations."""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from karpathy_wiki_ingest.state import (
    JOB_DEAD,
    JOB_LEASED,
    JOB_PENDING,
    JOB_SUCCEEDED,
    JOB_SUPERSEDED,
    STATE_VERSION,
    Lease,
    StateError,
    StateStore,
)

MANIFEST_REVISION = "a" * 64


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ingest.sqlite3"


class OpenTests(StoreTestCase):
    def test_open_creates_a_versioned_schema_and_reopening_is_idempotent(self):
        store = StateStore.open(self.path)
        store.close()
        connection = StateStore.open(self.path)._connection
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.close()
        self.assertEqual(version, STATE_VERSION)

        reopened = StateStore.open(self.path)
        reopened.close()

    def test_a_newer_store_is_rejected(self):
        StateStore.open(self.path).close()
        raw = sqlite3.connect(self.path)
        raw.execute("PRAGMA user_version=99")
        raw.close()
        with self.assertRaises(ValueError):
            StateStore.open(self.path)

    def test_open_requires_a_path(self):
        with self.assertRaises(ValueError):
            StateStore.open("")


class JobTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store = StateStore.open(self.path)
        self.addCleanup(self.store.close)

    def test_enqueue_validates_input(self):
        with self.assertRaises(ValueError):
            self.store.enqueue("unknown", {})
        with self.assertRaises(ValueError):
            self.store.enqueue("ingest", [])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.store.enqueue("ingest", {"bad": object()})
        with self.assertRaises(ValueError):
            self.store.enqueue("ingest", {}, idempotency_key="")

    def test_enqueue_without_key_creates_independent_jobs(self):
        first, created = self.store.enqueue("ingest", {"provider": "webdav"})
        second, created = self.store.enqueue("ingest", {"provider": "webdav"})
        self.assertTrue(created)
        self.assertNotEqual(first.id, second.id)

    def test_enqueue_is_idempotent_for_a_key(self):
        first, created = self.store.enqueue(
            "ingest", {"provider": "webdav"}, idempotency_key="webdav-generation:abc", now=100
        )
        self.assertTrue(created)
        self.assertEqual(first.state, JOB_PENDING)

        second, created = self.store.enqueue(
            "ingest", {"provider": "webdav"}, idempotency_key="webdav-generation:abc", now=200
        )
        self.assertFalse(created)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.created_at, 100)

    def test_job_lookup(self):
        job, _ = self.store.enqueue("ingest", {"provider": "webdav"}, idempotency_key="k")
        self.assertEqual(self.store.job(job.id).id, job.id)
        found = self.store.job_by_idempotency_key("k")
        assert found is not None
        self.assertEqual(found.id, job.id)
        self.assertIsNone(self.store.job_by_idempotency_key("missing"))
        with self.assertRaises(ValueError):
            self.store.job("missing")

    def test_state_transitions_are_recorded(self):
        job, _ = self.store.enqueue("ingest", {}, now=100)
        lease = self.store.claim(job.id, "holder", lease_seconds=60, now=110)
        assert lease is not None
        self.store.complete(lease.id, result={"generation": "g"}, now=120)
        events = self.store.job_events(job.id)
        self.assertEqual(
            [(event["from_state"], event["to_state"], event["at"]) for event in events],
            [
                (None, JOB_PENDING, 100),
                (JOB_PENDING, JOB_LEASED, 110),
                (JOB_LEASED, JOB_SUCCEEDED, 120),
            ],
        )


class ClaimTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store = StateStore.open(self.path)
        self.addCleanup(self.store.close)
        self.job, _ = self.store.enqueue("ingest", {"provider": "webdav"}, now=1000)

    def test_claim_moves_a_pending_job_to_leased(self):
        lease = self.store.claim(self.job.id, "holder", lease_seconds=60, now=1000)
        assert lease is not None
        self.assertEqual(lease.job_id, self.job.id)
        self.assertEqual(lease.holder, "holder")
        self.assertEqual(lease.expires_at, 1060)
        self.assertEqual(self.store.job(self.job.id).state, JOB_LEASED)
        self.assertEqual(self.store.job(self.job.id).attempts, 1)

    def test_a_leases_job_cannot_be_claimed_again(self):
        self.store.claim(self.job.id, "holder", lease_seconds=60, now=1000)
        self.assertIsNone(self.store.claim(self.job.id, "other", lease_seconds=60, now=1001))

    def test_backoff_defers_the_next_claim(self):
        lease = self.store.claim(self.job.id, "holder", lease_seconds=60, now=1000)
        assert lease is not None
        failed = self.store.fail(lease.id, "quarantined:1", base_backoff_seconds=60, now=1000)
        self.assertEqual(failed.state, JOB_PENDING)
        self.assertEqual(failed.next_attempt_at, 1060)
        self.assertEqual(failed.last_error, "quarantined:1")
        self.assertIsNone(self.store.claim(self.job.id, "holder", lease_seconds=60, now=1059))
        retry = self.store.claim(self.job.id, "holder", lease_seconds=60, now=1060)
        assert retry is not None
        self.assertEqual(self.store.job(self.job.id).attempts, 2)

    def test_backoff_is_capped(self):
        lease = self.store.claim(self.job.id, "holder", lease_seconds=60, now=1000)
        assert lease is not None
        failed = self.store.fail(
            lease.id,
            "error",
            base_backoff_seconds=600,
            max_backoff_seconds=900,
            max_attempts=10,
            now=1000,
        )
        self.assertEqual(failed.next_attempt_at, 1600)

    def test_exhausted_attempts_become_dead(self):
        for attempt in range(5):
            now = 1000 + attempt * 100
            lease = self.store.claim(self.job.id, "holder", lease_seconds=60, now=now)
            assert lease is not None
            dead = self.store.fail(
                lease.id,
                "quarantined:1",
                base_backoff_seconds=10,
                max_attempts=5,
                now=now,
            )
        self.assertEqual(dead.state, JOB_DEAD)
        self.assertIsNone(self.store.claim(self.job.id, "holder", lease_seconds=60, now=2000))

    def test_rearm_makes_a_dead_job_claimable_again(self):
        for attempt in range(5):
            now = 1000 + attempt * 100
            lease = self.store.claim(self.job.id, "holder", lease_seconds=60, now=now)
            assert lease is not None
            self.store.fail(
                lease.id, "quarantined:1", base_backoff_seconds=10, max_attempts=5, now=now
            )
        rearmed = self.store.rearm(self.job.id, now=3000)
        self.assertEqual(rearmed.state, JOB_PENDING)
        self.assertEqual(rearmed.attempts, 0)
        self.assertIsNone(rearmed.last_error)
        lease = self.store.claim(self.job.id, "holder", lease_seconds=60, now=3000)
        assert lease is not None

    def test_rearm_rejects_non_terminal_jobs(self):
        with self.assertRaises(StateError):
            self.store.rearm(self.job.id)

    def test_heartbeat_extends_the_lease(self):
        lease = self.store.claim(self.job.id, "holder", lease_seconds=60, now=1000)
        assert lease is not None
        extended = self.store.heartbeat(lease.id, lease_seconds=120, now=1050)
        self.assertEqual(extended.expires_at, 1170)

    def test_an_expired_lease_cannot_be_renewed(self):
        lease = self.store.claim(self.job.id, "holder", lease_seconds=60, now=1000)
        assert lease is not None
        with self.assertRaises(StateError):
            self.store.heartbeat(lease.id, lease_seconds=60, now=1100)
        # The expired lease is recovered only through expire_leases.
        self.assertEqual(self.store.expire_leases(now=1100), 1)
        recovered = self.store.claim(self.job.id, "holder", lease_seconds=60, now=1100)
        assert recovered is not None

    def test_heartbeat_requires_a_known_lease(self):
        with self.assertRaises(StateError):
            self.store.heartbeat("missing", lease_seconds=60)


class CompleteTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store = StateStore.open(self.path)
        self.addCleanup(self.store.close)
        self.job, _ = self.store.enqueue("ingest", {"provider": "webdav"}, now=1000)
        lease = self.store.claim(self.job.id, "holder", lease_seconds=600, now=1000)
        assert lease is not None
        self.lease_id = lease.id

    def test_complete_finishes_the_job_once(self):
        done = self.store.complete(self.lease_id, result={"generation": "g"}, now=1100)
        self.assertEqual(done.state, JOB_SUCCEEDED)
        self.assertEqual(done.result, {"generation": "g"})
        with self.assertRaises(ValueError):
            self.store.lease(self.lease_id)
        with self.assertRaises(StateError):
            self.store.complete(self.lease_id)

    def test_complete_and_fail_reject_an_expired_lease(self):
        # Completing before the expiry is still valid authority.
        done = self.store.complete(self.lease_id, now=1059)
        self.assertEqual(done.state, JOB_SUCCEEDED)

        second, _ = self.store.enqueue("ingest", {}, now=1000)
        lease = self.store.claim(second.id, "holder", lease_seconds=60, now=1000)
        assert lease is not None
        with self.assertRaises(StateError):
            self.store.complete(lease.id, now=1060)
        # The lease still exists and only expiry recovery can reclaim it.
        self.assertEqual(self.store.expire_leases(now=1100), 1)
        self.assertEqual(self.store.job(second.id).state, JOB_PENDING)

        third, _ = self.store.enqueue("ingest", {}, now=1000)
        lease = self.store.claim(third.id, "holder", lease_seconds=60, now=1000)
        assert lease is not None
        with self.assertRaises(StateError):
            self.store.fail(lease.id, "late failure", now=1060)
        self.assertEqual(self.store.job(third.id).state, JOB_LEASED)

    def test_complete_requires_the_lease_of_a_leased_job(self):
        with self.assertRaises(StateError):
            self.store.complete("not-a-lease")

    def test_fail_requires_the_lease_of_a_leased_job(self):
        self.store.complete(self.lease_id, now=1100)
        with self.assertRaises(StateError):
            self.store.fail(self.lease_id, "late failure", now=1100)


class RecoveryTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store = StateStore.open(self.path)
        self.addCleanup(self.store.close)
        self.job, _ = self.store.enqueue("ingest", {}, now=1000)

    def test_an_expired_lease_returns_the_job_to_pending(self):
        self.store.claim(self.job.id, "holder", lease_seconds=60, now=1000)
        self.assertEqual(self.store.expire_leases(now=1060), 1)
        recovered = self.store.job(self.job.id)
        self.assertEqual(recovered.state, JOB_PENDING)
        self.assertEqual(recovered.next_attempt_at, 1060)
        self.assertEqual(self.store.job(self.job.id).attempts, 1)
        lease = self.store.claim(self.job.id, "holder", lease_seconds=60, now=1060)
        assert lease is not None

    def test_a_live_lease_is_not_expired(self):
        self.store.claim(self.job.id, "holder", lease_seconds=60, now=1000)
        self.assertEqual(self.store.expire_leases(now=1001), 0)
        self.assertEqual(self.store.job(self.job.id).state, JOB_LEASED)


class GenerationTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store = StateStore.open(self.path)
        self.addCleanup(self.store.close)

    def test_source_generations_are_recorded_once(self):
        self.assertTrue(
            self.store.record_source_generation(
                "webdav",
                "20260101T000000Z-abcdef01",
                manifest_revision=MANIFEST_REVISION,
                item_count=3,
                redaction_fingerprint="fp",
                now=100,
            )
        )
        self.assertFalse(
            self.store.record_source_generation(
                "webdav",
                "20260101T000000Z-abcdef01",
                manifest_revision=MANIFEST_REVISION,
                item_count=3,
                now=200,
            )
        )

    def test_source_generation_input_is_validated(self):
        with self.assertRaises(ValueError):
            self.store.record_source_generation(
                "webdav", "gen", manifest_revision="nothex", item_count=1
            )
        with self.assertRaises(ValueError):
            self.store.record_source_generation(
                "webdav", "gen", manifest_revision=MANIFEST_REVISION, item_count=-1
            )

    def test_publications_are_recorded_once_per_key(self):
        self.assertTrue(
            self.store.record_publication(
                "publish:one",
                "webdav",
                "20260101T000000Z-abcdef01",
                wiki_base_revision="b" * 64,
                commit="c" * 40,
                now=100,
            )
        )
        self.assertFalse(
            self.store.record_publication(
                "publish:one",
                "webdav",
                "20260101T000000Z-abcdef01",
                wiki_base_revision="b" * 64,
                commit="c" * 40,
                now=200,
            )
        )

    def test_publication_input_is_validated(self):
        with self.assertRaises(ValueError):
            self.store.record_publication("k", "webdav", "gen", status="unknown")
        with self.assertRaises(ValueError):
            self.store.record_publication("k", "webdav", "gen", wiki_base_revision="abc")
        with self.assertRaises(ValueError):
            self.store.record_publication("k", "webdav", "gen", commit="z" * 40)
        with self.assertRaises(ValueError):
            self.store.record_publication("k", "webdav", "gen", job_id="missing")
        self.assertTrue(self.store.record_publication("k", "webdav", "gen", job_id=None, now=100))

    def test_publication_can_reference_a_job(self):
        job, _ = self.store.enqueue("publish", {}, now=100)
        self.assertTrue(self.store.record_publication("k", "webdav", "gen", job_id=job.id, now=100))


class SupersedeTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store = StateStore.open(self.path)
        self.addCleanup(self.store.close)

    def test_supersede_moves_a_pending_job_to_a_terminal_state(self):
        job, _ = self.store.enqueue("ingest", {"provider": "webdav"}, now=1000)
        superseded = self.store.supersede(job.id, now=1100)
        self.assertEqual(superseded.state, JOB_SUPERSEDED)
        self.assertIsNone(self.store.claim(job.id, "holder", lease_seconds=60, now=1100))
        with self.assertRaises(StateError):
            self.store.supersede(job.id, now=1200)
        # A superseded job whose content becomes active again is rearmed.
        rearmed = self.store.rearm(job.id, now=1200)
        self.assertEqual(rearmed.state, JOB_PENDING)

    def test_pending_jobs_lists_only_pending_jobs_of_a_type(self):
        first, _ = self.store.enqueue("ingest", {"provider": "webdav"}, now=1000)
        self.store.enqueue("publish", {}, now=1000)
        second, _ = self.store.enqueue("ingest", {"provider": "webdav"}, now=1100)
        self.store.supersede(first.id, now=1100)
        pending = self.store.pending_jobs("ingest")
        self.assertEqual([job.id for job in pending], [second.id])


class MetricsTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store = StateStore.open(self.path)
        self.addCleanup(self.store.close)

    def test_metrics_report_queue_depth_and_age(self):
        self.assertEqual(
            self.store.metrics(now=1000),
            {
                "jobs": {
                    JOB_PENDING: 0,
                    JOB_LEASED: 0,
                    JOB_SUCCEEDED: 0,
                    JOB_DEAD: 0,
                    JOB_SUPERSEDED: 0,
                },
                "oldest_pending_age": 0,
                "queue_depth": 0,
                "source_generations": 0,
                "publications": 0,
                "retried_jobs": 0,
                "failed_jobs": 0,
                "last_source_generation": None,
                "last_publication": None,
            },
        )
        job, _ = self.store.enqueue("ingest", {}, now=1000)
        metrics = self.store.metrics(now=1100)
        self.assertEqual(metrics["jobs"][JOB_PENDING], 1)
        self.assertEqual(metrics["queue_depth"], 1)
        self.assertEqual(metrics["oldest_pending_age"], 100)

        # The pending age is measured from acceptance: a job that is only
        # waiting out its retry backoff keeps its original age.
        lease = self.store.claim(job.id, "holder", lease_seconds=60, now=1100)
        assert lease is not None
        failed = self.store.fail(lease.id, "error", base_backoff_seconds=600, now=1100)
        self.assertEqual(failed.next_attempt_at, 1700)
        self.assertEqual(self.store.metrics(now=2000)["oldest_pending_age"], 1000)
        self.store.record_source_generation(
            "webdav", "gen", manifest_revision=MANIFEST_REVISION, item_count=1, now=1100
        )
        self.store.record_publication("k", "webdav", "gen", commit="c" * 40, now=1100)
        metrics = self.store.metrics(now=1100)
        self.assertEqual(metrics["source_generations"], 1)
        self.assertEqual(metrics["publications"], 1)
        self.assertEqual(metrics["failed_jobs"], 1)
        self.assertEqual(metrics["retried_jobs"], 0)
        self.assertEqual(
            metrics["last_source_generation"],
            {"provider": "webdav", "generation_id": "gen", "recorded_at": 1100},
        )
        self.assertEqual(
            metrics["last_publication"],
            {
                "provider": "webdav",
                "source_generation_id": "gen",
                "commit_id": "c" * 40,
                "completed_at": 1100,
            },
        )

    def test_retries_and_uncommitted_publications_are_not_reported_as_success(self):
        job, _ = self.store.enqueue("ingest", {}, now=100)
        first = self.store.claim(job.id, "worker", now=100)
        assert first is not None
        self.store.fail(first.id, "operational:OSError", base_backoff_seconds=1, now=100)
        retry = self.store.claim(job.id, "worker", now=101)
        assert retry is not None
        self.store.complete(retry.id, now=102)
        self.store.record_publication("uncommitted", "paperless", "gen", now=102)
        metrics = self.store.metrics(now=103)
        self.assertEqual(metrics["retried_jobs"], 1)
        self.assertEqual(metrics["failed_jobs"], 0)
        self.assertIsNone(metrics["last_publication"])


class PublisherLeaseTests(StoreTestCase):
    def test_only_one_publish_job_can_be_leased_and_recovery_fences_old_holder(self):
        first = StateStore.open(self.path)
        second = StateStore.open(self.path)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        a, _ = first.enqueue("publish", {}, now=1000)
        b, _ = second.enqueue("publish", {}, now=1000)
        ingest, _ = first.enqueue("ingest", {}, now=1000)

        original = first.claim(a.id, "a", lease_seconds=60, now=1000)
        assert original is not None
        self.assertIsNone(second.claim(b.id, "b", lease_seconds=60, now=1001))
        self.assertIsNotNone(second.claim(ingest.id, "ingestor", now=1001))
        first.heartbeat(original.id, lease_seconds=60, now=1050)
        self.assertIsNone(second.claim(b.id, "b", lease_seconds=60, now=1060))

        # The expired holder is fenced even if recovery has not run yet.
        replacement = second.claim(b.id, "b", lease_seconds=60, now=1110)
        assert replacement is not None
        with self.assertRaises(StateError):
            first.complete(original.id, now=1110)
        self.assertEqual(first.expire_leases(now=1110), 1)
        self.assertIsNone(first.claim(a.id, "a", now=1111))
        second.complete(replacement.id, now=1111)
        self.assertIsNotNone(first.claim(a.id, "a", now=1112))

    def test_concurrent_publishers_contending_for_distinct_jobs(self):
        setup = StateStore.open(self.path)
        try:
            jobs = [setup.enqueue("publish", {}, now=1000)[0] for _ in range(2)]
        finally:
            setup.close()
        leases: list[Lease | None] = []

        def claim(job_id: str) -> None:
            store = StateStore.open(self.path)
            try:
                leases.append(store.claim(job_id, job_id, lease_seconds=60, now=1000))
            finally:
                store.close()

        threads = [threading.Thread(target=claim, args=(job.id,)) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(lease is not None for lease in leases), 1)


class ConcurrentClaimTests(StoreTestCase):
    def test_exactly_one_writer_wins_the_job(self):
        setup = StateStore.open(self.path)
        try:
            job, _ = setup.enqueue("ingest", {}, now=1000)
        finally:
            setup.close()

        leases: list[Lease | None] = []

        def claim(holder: str) -> None:
            store = StateStore.open(self.path)
            try:
                leases.append(store.claim(job.id, holder, lease_seconds=60, now=1000))
            finally:
                store.close()

        threads = [threading.Thread(target=claim, args=(holder,)) for holder in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        claimed = [lease for lease in leases if lease is not None]
        self.assertEqual(len(claimed), 1)
        verify = StateStore.open(self.path)
        try:
            leased = verify.job(job.id)
            self.assertEqual(leased.state, JOB_LEASED)
            self.assertEqual(leased.attempts, 1)
        finally:
            verify.close()


if __name__ == "__main__":
    unittest.main()
