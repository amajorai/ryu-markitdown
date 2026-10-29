"""Job lifecycle regressions for the production in-process store."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ryu_markitdown import jobs


def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before the deadline")


class JobStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = jobs.JobStore()
        self.source = Path(tempfile.mkstemp(prefix="markitdown-job-")[1])
        self.source.write_text("source", encoding="utf-8")

    def tearDown(self) -> None:
        self.source.unlink(missing_ok=True)

    def test_failure_can_be_retried_without_mutating_the_failed_record(self) -> None:
        attempts = 0

        def parse(*_args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise jobs.ParseError("parse_failed", "converter unavailable")
            return {"markdown": "recovered"}

        with patch.object(jobs, "parse_file", side_effect=parse):
            failed = self.store.submit(self.source, "report.md", {})
            wait_for(lambda: failed.status in jobs.TERMINAL)
            retried = self.store.submit(self.source, "report.md", {})
            wait_for(lambda: retried.status in jobs.TERMINAL)

        self.assertEqual(2, attempts)
        self.assertEqual("failed", failed.status)
        self.assertEqual("parse_failed", failed.error_code)
        self.assertIsNone(failed.result)
        self.assertEqual("succeeded", retried.status)
        self.assertEqual({"markdown": "recovered"}, retried.result)

    def test_running_cancel_is_idempotent_and_discards_late_result(self) -> None:
        release = threading.Event()

        def parse(*_args):
            release.wait(timeout=2)
            return {"markdown": "must not be published"}

        with patch.object(jobs, "parse_file", side_effect=parse):
            job = self.store.submit(self.source, "report.md", {})
            wait_for(lambda: job.status == "running")
            first = self.store.cancel(job.id)
            second = self.store.cancel(job.id)
            cancelled_at = job.finished_at
            release.set()
            wait_for(lambda: job._cancel.is_set())
            time.sleep(0.3)

        self.assertIs(first, job)
        self.assertIs(second, job)
        self.assertEqual("cancelled", job.status)
        self.assertEqual(cancelled_at, job.finished_at)
        self.assertIsNone(job.result)

    def test_completed_burst_is_reclaimed_without_a_followup_submission(self) -> None:
        release = threading.Event()

        def parse(*_args):
            release.wait(timeout=2)
            return {"markdown": "ok"}

        with (
            patch.object(jobs, "MAX_JOBS", 4),
            patch.object(jobs, "parse_file", side_effect=parse),
        ):
            submitted = [
                self.store.submit(self.source, f"report-{index}.md", {})
                for index in range(9)
            ]
            self.assertEqual(9, len(self.store.list()))
            release.set()
            wait_for(lambda: all(job.status in jobs.TERMINAL for job in submitted))
            wait_for(lambda: len(self.store.list()) <= 4)

        retained = self.store.list()
        self.assertEqual(4, len(retained))
        self.assertTrue(all(job.status == "succeeded" for job in retained))


if __name__ == "__main__":
    unittest.main()
