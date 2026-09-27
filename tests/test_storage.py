"""State files shared by every server process on the machine.

An MCP client starts one server per session, so several processes read and
write the ledger, the account meter and the advisor store at once. Writes
must not lose each other's records.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from wisdomtooth import accounts, advisors, storage, usage

ROOT = Path(__file__).resolve().parent.parent


def test_write_atomic_replaces_the_whole_file(tmp_path):
    path = tmp_path / "state.json"
    storage.write_atomic(str(path), "first")
    storage.write_atomic(str(path), "second")
    assert path.read_text(encoding="utf-8") == "second"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_private_files_are_owner_only(tmp_path):
    path = tmp_path / "secret.json"
    storage.write_atomic(str(path), "{}", private=True)
    assert path.stat().st_mode & 0o777 == 0o600
    line = tmp_path / "ledger.jsonl"
    storage.append_line(str(line), "{}", private=True)
    assert line.stat().st_mode & 0o777 == 0o600


def test_the_lock_serialises_read_modify_write(tmp_path):
    path = str(tmp_path / "counter.txt")
    storage.write_atomic(path, "0")

    def bump():
        for _ in range(20):
            with storage.locked(path):
                value = int(Path(path).read_text(encoding="utf-8"))
                time.sleep(0.001)
                storage.write_atomic(path, str(value + 1))

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Path(path).read_text(encoding="utf-8") == "80"


_APPENDER = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
from wisdomtooth import usage
usage.LEDGER_MAX_BYTES = 2000   # compact constantly
for i in range(int(sys.argv[3])):
    usage.append(sys.argv[2], {"ts": time.time(), "who": sys.argv[4], "i": i})
'''


def test_processes_appending_to_one_ledger_lose_nothing(tmp_path):
    """Compaction rewrites the file; without a lock, another process's append
    that lands during the rewrite is lost."""
    ledger = tmp_path / "usage.jsonl"
    script = tmp_path / "appender.py"
    script.write_text(_APPENDER, encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(script), str(ROOT),
                               str(ledger), "60", str(n)])
             for n in range(4)]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    records = usage.read_ledger(str(ledger))
    assert len(records) == 240


def test_two_meters_do_not_overwrite_each_others_readings(tmp_path):
    """Two server processes each hold a Meter; the second one to write must
    not replace the first one's reading with its stale copy."""
    path = str(tmp_path / "accounts.json")
    first = accounts.Meter(lambda: path)
    second = accounts.Meter(lambda: path)
    first.credit("x"), second.credit("x")  # both have read the empty file
    first.note_credit("openrouter", {"limit": 10, "limit_remaining": 4})
    second.note_headers("gpt", {"x-ratelimit-limit-tokens": "100",
                                "x-ratelimit-remaining-tokens": "5"})
    fresh = accounts.Meter(lambda: path)
    assert fresh.credit("openrouter")["remaining"] == 4
    assert fresh.ratelimit("gpt")["remaining"] == 5
    assert first.ratelimit("gpt")["remaining"] == 5  # sees the other's write


def test_concurrent_store_updates_keep_every_advisor(tmp_path):
    path = str(tmp_path / "advisors.json")

    def add(name):
        def change(store):
            time.sleep(0.01)
            store.setdefault("advisors", {})[name] = {"provider": "ollama",
                                                      "model": "m"}
        advisors.update_store(path, change, warn=lambda m: None)

    threads = [threading.Thread(target=add, args=(f"a{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stored = json.loads(Path(path).read_text(encoding="utf-8"))["advisors"]
    assert sorted(stored) == [f"a{i}" for i in range(6)]
