import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.db import MemoryStore, x_gate_decision
from core.xgate import local_next_allowed, record_local_start

MON = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_decision_boundaries():
    assert x_gate_decision(None, MON) == (True, None)
    assert x_gate_decision(MON, MON + timedelta(hours=48))[0]
    assert not x_gate_decision(MON, MON + timedelta(hours=47, minutes=59))[0]
    assert x_gate_decision(MON, MON + timedelta(hours=1), forced=True)[0]


def test_monday_run_blocks_tuesday_and_wednesday_morning():
    clock = Clock(MON)
    store = MemoryStore(clock=clock)
    assert store.try_start_x_run().allowed
    for hours in (24, 47):  # Tuesday, Wednesday (daily checks)
        clock.now = MON + timedelta(hours=hours)
        result = store.try_start_x_run()
        assert not result.allowed
        assert result.next_allowed == MON + timedelta(hours=48)
    clock.now = MON + timedelta(hours=48)
    assert store.try_start_x_run().allowed


def test_failed_run_still_counts():
    clock = Clock(MON)
    store = MemoryStore(clock=clock)
    gate = store.try_start_x_run()
    store.finish_run(gate.run_id, "failed", {}, "search restricted")
    clock.now = MON + timedelta(hours=30)
    assert not store.try_start_x_run().allowed


def test_overlapping_runs_only_one_starts():
    store = MemoryStore(clock=Clock(MON))
    results = []
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        results.append(store.try_start_x_run().allowed)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_forced_run_is_recorded():
    store = MemoryStore(clock=Clock(MON))
    store.try_start_x_run()
    gate = store.try_start_x_run(forced=True)
    assert gate.allowed
    assert store.runs[-1]["forced"] is True


def test_local_backup_file(tmp_path: Path):
    stamp = tmp_path / "x_last_start.txt"
    assert local_next_allowed(stamp, MON) is None
    record_local_start(stamp, MON)
    assert local_next_allowed(stamp, MON + timedelta(hours=47)) == MON + timedelta(hours=48)
    assert local_next_allowed(stamp, MON + timedelta(hours=48)) is None
    stamp.write_text("garbage")
    assert local_next_allowed(stamp, MON) is None
