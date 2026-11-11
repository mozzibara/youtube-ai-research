"""Database fault injection: prove one transaction guards lease, tokens and 429."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from threading import Thread
import time
import unittest

from ytresearch.batch_budget import (
    batch_budget_status,
)
from ytresearch.batch_queue import (
    claim_batch_job, finish_batch_job, batch_queue_status,
)
from ytresearch.batch_stress import _create_isolated_fixture, MODEL


class FaultInjectedQueueTransactions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sha, self.index_sha = _create_isolated_fixture(self.root)
        self.dbfile = self.root / "data" / "batch_dryrun_queue.sqlite3"
        self.t0 = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
        self.usage = {
            "uncached_input_tokens": 200, "cache_read_tokens": 0,
            "output_tokens": 20,
        }

    def _trigger(self, name: str, event: str, table: str, failure: str):
        with closing(sqlite3.connect(self.dbfile)) as db:
            db.execute(
                f"CREATE TRIGGER {name} BEFORE {event} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{failure}'); END"
            )
            db.commit()

    def _drop(self, name):
        with closing(sqlite3.connect(self.dbfile)) as db:
            db.execute(f"DROP TRIGGER {name}")
            db.commit()

    def _claim(self):
        return claim_batch_job(
            self.root, self.sha, owner="fault-worker",
            model=MODEL, now_utc=self.t0,
        )

    def _status(self):
        return batch_budget_status(self.root,self.sha,now_utc=self.t0)

    def test_lease_update_failure_rolls_back_budget_and_rate_reservation(self):
        self._trigger(
            "fault_lease", "UPDATE OF status", "queue_jobs",
            "INJECTED_LEASE_UPDATE_FAILURE",
        )
        with self.assertRaisesRegex(sqlite3.DatabaseError, "INJECTED_LEASE_UPDATE_FAILURE"):
            self._claim()
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],{"PENDING":3})
        status=self._status()
        self.assertEqual(status["global"]["used_and_reserved_tokens"],0)
        self.assertEqual(status["global"]["unsettled_reservations"],0)
        self.assertEqual(status["rate_limit"]["requests_in_window"],0)
        self._drop("fault_lease")
        self.assertEqual(self._claim()["video_id"],f"{1:011d}")

    def test_budget_settlement_failure_preserves_lease_and_held_reserve(self):
        first=self._claim()
        self._trigger(
            "fault_settle","UPDATE OF status","budget_reservations",
            "INJECTED_SETTLEMENT_FAILURE",
        )
        with self.assertRaisesRegex(sqlite3.DatabaseError,"INJECTED_SETTLEMENT_FAILURE"):
            finish_batch_job(
                self.root,self.sha,video_id=first["video_id"],
                lease_token=first["lease_token"],success=True,
                usage=self.usage,now_utc=self.t0+timedelta(seconds=1),
            )
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],
                         {"LEASED":1,"PENDING":2})
        self.assertEqual(self._status()["global"]["unsettled_reservations"],1)
        self._drop("fault_settle")
        finished=finish_batch_job(
            self.root,self.sha,video_id=first["video_id"],
            lease_token=first["lease_token"],success=True,
            usage=self.usage,now_utc=self.t0+timedelta(seconds=1),
        )
        self.assertEqual(finished["status"],"SIMULATED")
        self.assertEqual(self._status()["global"]["unsettled_reservations"],0)

    def test_pause_failure_rolls_back_any_prior_usage_settlement(self):
        first=self._claim()
        self._trigger(
            "fault_pause","INSERT","global_rate_state",
            "INJECTED_GLOBAL_PAUSE_FAILURE",
        )
        with self.assertRaisesRegex(sqlite3.DatabaseError,"INJECTED_GLOBAL_PAUSE_FAILURE"):
            finish_batch_job(
                self.root,self.sha,video_id=first["video_id"],
                lease_token=first["lease_token"],success=False,
                usage=self.usage,rate_error_code="429",
                retry_after_seconds=60,
                now_utc=self.t0+timedelta(seconds=1),
            )
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],
                         {"LEASED":1,"PENDING":2})
        self.assertEqual(self._status()["global"]["unsettled_reservations"],1)
        self.assertFalse(self._status()["rate_limit"]["global_paused"])
        self._drop("fault_pause")
        result=finish_batch_job(
            self.root,self.sha,video_id=first["video_id"],
            lease_token=first["lease_token"],success=False,
            rate_error_code="429",retry_after_seconds=60,
            now_utc=self.t0+timedelta(seconds=1),
        )
        self.assertTrue(result["global_rate_paused"])
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],
                         {"PAUSED_FAILURE":1,"PENDING":2})

    def test_sqlite_writer_lock_waits_then_one_claim_commits(self):
        conn=sqlite3.connect(self.dbfile,timeout=1,isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("BEGIN EXCLUSIVE")
        result=[]
        def contender():
            try:
                result.append(self._claim())
            except Exception as exc:
                result.append(exc)
        thread=Thread(target=contender,daemon=True)
        thread.start()
        time.sleep(.15)
        conn.commit()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(),"Worker failed to release after SQLite writer lock")
        self.assertEqual(len(result),1)
        if isinstance(result[0],Exception):
            raise result[0]
        self.assertEqual(result[0]["video_id"],f"{1:011d}")
        self.assertEqual(self._status()["global"]["unsettled_reservations"],1)
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],
                         {"LEASED":1,"PENDING":2})


if __name__ == "__main__":
    unittest.main()
