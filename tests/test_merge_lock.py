"""The merge lease and CAS are checked under the same lock as the write."""
import argparse
import json
import multiprocessing

from orda2 import orda2_cli as cli
from orda2.orda2_migration import init_home
from orda2.orda2_store import Lock, Store


def _merge_worker(home, branch, barrier, results):
    class SynchronizedLock(Lock):
        first_entry = True

        def __enter__(self):
            if SynchronizedLock.first_entry:
                SynchronizedLock.first_entry = False
                barrier.wait(timeout=15)
            return super().__enter__()

    cli.Lock = SynchronizedLock
    try:
        results.put(cli.cmd_merge(argparse.Namespace(
            home=home, session="seat", proposal=branch,
        )))
    except Exception as exc:
        results.put(repr(exc))


def test_concurrent_merges_cannot_both_accept_same_record(tmp_path):
    home = str(tmp_path / "store")
    init_home(home)
    for session in ("writer-a", "writer-b"):
        assert cli.main(["worktree", "--home", home, "new", "--session", session]) == 0
        batch = tmp_path / f"{session}.json"
        batch.write_text(json.dumps([{"slug": "same-record", "summary": session}]))
        assert cli.main(["propose", "--home", home, "--session", session, "--file", str(batch)]) == 0
    Store(home).claim("seat")
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [context.Process(target=_merge_worker, args=(
        home, f"orda/prop/{session}/1", barrier, results,
    )) for session in ("writer-a", "writer-b")]
    try:
        for worker in workers:
            worker.start()
        verdicts = [results.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
        assert sorted(verdicts) == [0, 3], verdicts
        assert Store(home).verify()["ok"]
        events = Store(home).read_events()
        assert len([event for event in events if event.get("kind") == "update"]) == 1
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
