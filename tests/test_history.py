"""Tests for the --quiet history recorder (current.json, JSONL tiers) and `power-watch history`."""

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_power_watch import (
    SCRIPT,
    FakeClock,
    add_energy,
    cli_env,
    fake_notify,
    fake_smi,
    make_sysfs,
    no_user_config,  # noqa: F401 -- autouse fixture: never read the real user config
    pw,
    write,
)

BASE = 1_761_300_000.0  # divisible by 60 and by 900: bucket starts are easy to read
SCHEMA = ["t", "n", "avg", "max", "cpu", "gpu", "rest", "chg", "ac", "k"]


def rec(epoch, total=100.0, kind="estimated", ac=True, cpu=10.0, gpu=40.0, rest=25.0, chg=0.0):
    """A current.json-shaped sample, as History.record() receives it."""
    return {"total_w": total, "total_kind": kind, "ac_online": ac, "cpu_w": cpu, "gpu_w": gpu,
            "rest_w": rest, "charge_w": chg, "epoch": epoch}


def lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def ts(path):
    return [p["t"] for p in lines(path)]


@pytest.fixture
def history(tmp_path):
    h = pw.History(tmp_path / "state", 2.0, epoch=lambda: BASE)
    assert h.start() is True
    yield h
    h.close()


def raw(sdir):
    return Path(sdir) / "history-raw.jsonl"


def m1(sdir):
    return Path(sdir) / "history-1m.jsonl"


def m15(sdir):
    return Path(sdir) / "history-15m.jsonl"


# ------------------------------------------------------------- consolidation

def test_bucket_mixed_kinds_and_nulls():
    b = pw.Bucket(BASE)
    b.add(rec(BASE, total=80.0, cpu=10.0, gpu=None, chg=0.0, ac=True))
    b.add(rec(BASE + 2, total=100.0, cpu=20.0, gpu=30.0, chg=5.0, ac=False, kind="measured"))
    b.add(rec(BASE + 4, total=None, cpu=None, gpu=50.0, chg=None, ac=None, kind="measured"))
    line = b.line()
    assert list(line) == SCHEMA
    assert line == {"t": BASE, "n": 3, "avg": 90.0, "max": 100.0, "cpu": 15.0, "gpu": 40.0,
                    "rest": 25.0, "chg": 2.5, "ac": 0.5, "k": "x"}


def test_bucket_all_null_values():
    b = pw.Bucket(BASE)
    for i in range(2):
        b.add(rec(BASE + i, total=None, cpu=None, gpu=None, rest=None, chg=None, ac=None,
                  kind="measured"))
    assert b.line() == {"t": BASE, "n": 2, "avg": None, "max": None, "cpu": None, "gpu": None,
                        "rest": None, "chg": None, "ac": None, "k": "m"}


def test_bucket_kinds_and_rounding():
    est, meas = pw.Bucket(BASE), pw.Bucket(BASE)
    for total in (10.0, 10.0, 10.01):
        est.add(rec(BASE, total=total))
        meas.add(rec(BASE, total=total, kind="measured", ac=False))
    assert est.line()["k"] == "e" and meas.line()["k"] == "m"
    assert est.line()["avg"] == 10.0  # 10.00333 rounded to 2 decimals
    assert est.line()["ac"] == 1.0 and meas.line()["ac"] == 0.0
    ac = pw.Bucket(BASE)
    for value in (True, True, False):
        ac.add(rec(BASE, ac=value))
    assert ac.line()["ac"] == 0.67


def test_raw_line_is_a_one_sample_bucket(history):
    history.record(rec(BASE + 1.234, total=82.126, ac=False, kind="measured"))
    (line,) = lines(raw(history.sdir))
    assert list(line) == SCHEMA
    assert line == {"t": BASE + 1.23, "n": 1, "avg": 82.13, "max": 82.13, "cpu": 10.0,
                    "gpu": 40.0, "rest": 25.0, "chg": 0.0, "ac": 0.0, "k": "m"}


def test_bucket_boundaries_and_flush(history):
    sdir = history.sdir
    for offset, total in ((0, 60.0), (30, 80.0), (59.99, 100.0)):
        history.record(rec(BASE + offset, total=total))
    assert not m1(sdir).exists()  # the first minute is still open
    history.record(rec(BASE + 60, total=10.0))  # lands in the next minute: closes the first
    assert lines(m1(sdir)) == [{"t": BASE, "n": 3, "avg": 80.0, "max": 100.0, "cpu": 10.0,
                                "gpu": 40.0, "rest": 25.0, "chg": 0.0, "ac": 1.0, "k": "e"}]
    history.record(rec(BASE + 899.9, total=20.0))
    assert not m15(sdir).exists()
    history.record(rec(BASE + 900, total=30.0))
    assert ts(m1(sdir)) == [BASE, BASE + 60, BASE + 840]
    assert lines(m15(sdir))[0]["t"] == BASE and lines(m15(sdir))[0]["n"] == 5
    assert ts(raw(sdir)) == [BASE, BASE + 30, BASE + 59.99, BASE + 60, BASE + 899.9, BASE + 900]
    history.flush()  # clean shutdown: the open buckets (t = their start) are appended
    assert ts(m1(sdir))[-1] == BASE + 900 and ts(m15(sdir)) == [BASE, BASE + 900]
    assert lines(m15(sdir))[-1]["n"] == 1


def test_clock_going_backwards_drops_open_bucket_and_never_rewinds(history):
    # a step back of at most one raw step (2 s): nothing written is in the future, drop and go on
    sdir = history.sdir
    history.record(rec(BASE, total=10.0))
    history.record(rec(BASE + 60, total=20.0))   # closes minute BASE
    history.record(rec(BASE + 60.5, total=30.0))
    history.record(rec(BASE + 59, total=99.0))   # clock went back: open minute BASE+60 dropped
    history.record(rec(BASE + 70, total=40.0))   # closes the fresh minute BASE: t not > last, dropped
    history.flush()
    assert ts(raw(sdir)) == [BASE, BASE + 60, BASE + 60.5, BASE + 70]
    one_min = lines(m1(sdir))
    assert [(p["t"], p["n"], p["avg"]) for p in one_min] == [(BASE, 1, 10.0), (BASE + 60, 1, 40.0)]
    for path in (raw(sdir), m1(sdir), m15(sdir)):
        t = ts(path)
        assert t == sorted(set(t))


def test_restart_learns_last_t_from_tail_and_skips_torn_line(tmp_path):
    sdir = tmp_path / "state"
    sdir.mkdir()
    good = {"t": BASE + 10, "n": 1, "avg": 1.0}
    raw(sdir).write_text(json.dumps({"t": BASE, "n": 1}) + "\n" + json.dumps(good) + "\n"
                         + '{"t": 17613000')  # torn tail after a crash
    h = pw.History(sdir, 2.0, epoch=lambda: BASE + 20)
    assert h.start()
    try:
        assert h.last_t["raw"] == BASE + 10
        assert ts(raw(sdir)) == [BASE, BASE + 10]  # compaction dropped the torn line
        h.record(rec(BASE + 9))   # not after the last written t (and within a step): dropped
        h.record(rec(BASE + 12))
        assert ts(raw(sdir)) == [BASE, BASE + 10, BASE + 12]
    finally:
        h.close()


def test_forward_jump_then_correction_resumes_history(history):
    """A clock that jumped ahead and was corrected must not block history until real time
    catches up: the lines from the future are compacted away and appending resumes."""
    sdir = history.sdir
    jump = BASE + 86400
    for epoch in (BASE, BASE + 2, jump, jump + 60):   # jump + 60 closes the future minute
        history.record(rec(epoch))
    assert ts(raw(sdir))[-1] == jump + 60 and ts(m1(sdir)) == [BASE, jump]
    for epoch in (BASE + 4, BASE + 60, BASE + 120):   # clock corrected
        history.record(rec(epoch))
    history.flush()
    assert ts(raw(sdir)) == [BASE, BASE + 2, BASE + 4, BASE + 60, BASE + 120]
    assert ts(m1(sdir)) == [BASE, BASE + 60, BASE + 120]
    assert ts(m15(sdir)) == [BASE]
    for path in (raw(sdir), m1(sdir), m15(sdir)):
        t = ts(path)
        assert t == sorted(set(t)) and max(t) <= BASE + 120


def test_forward_jump_then_correction_across_restart(tmp_path):
    sdir = tmp_path / "state"
    jump = BASE + 86400
    first = pw.History(sdir, 2.0, epoch=lambda: jump + 60)
    assert first.start()
    for epoch in (BASE, BASE + 2, jump, jump + 60):
        first.record(rec(epoch))
    first.flush()
    first.close()
    assert ts(m1(sdir)) == [BASE, jump, jump + 60] and ts(m15(sdir)) == [BASE, jump]
    second = pw.History(sdir, 2.0, epoch=lambda: BASE + 4)  # restarted after the correction
    assert second.start()
    try:
        assert second.last_t == {"raw": BASE + 2, "1m": BASE, "15m": BASE}
        assert ts(m1(sdir)) == [BASE] and ts(m15(sdir)) == [BASE]
        second.record(rec(BASE + 4))
        assert ts(raw(sdir)) == [BASE, BASE + 2, BASE + 4]  # appended right away
    finally:
        second.close()


def test_last_point_t_reads_only_the_tail(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text("".join(json.dumps({"t": float(i)}) + "\n" for i in range(5000)) + "garbage")
    assert pw.last_point_t(path) == 4999.0
    assert pw.last_point_t(tmp_path / "missing") is None


# ------------------------------------------------------ retention, compaction

def test_startup_compaction_applies_retention(tmp_path):
    sdir = tmp_path / "state"
    sdir.mkdir()
    now = BASE + 100 * 86400
    raw(sdir).write_text("".join(json.dumps({"t": now - age}) + "\n"
                                 for age in (7200, 3600, 3599, 10)) + "not json\n")
    m1(sdir).write_text("".join(json.dumps({"t": now - h * 3600}) + "\n" for h in (49, 47)))
    m15(sdir).write_text("".join(json.dumps({"t": now - d * 86400}) + "\n" for d in (15, 13)))
    h = pw.History(sdir, 2.0, epoch=lambda: now)
    assert h.start()
    h.close()
    assert ts(raw(sdir)) == [now - 3599, now - 10]
    assert ts(m1(sdir)) == [now - 47 * 3600]
    assert ts(m15(sdir)) == [now - 13 * 86400]
    assert sorted(p.name for p in sdir.iterdir()) == [
        "history-15m.jsonl", "history-1m.jsonl", "history-raw.jsonl", "history.lock"]


def test_runtime_compaction_when_over_1_25x_retention(tmp_path):
    # interval 600 s -> raw retention is 6 points; compaction once more than 7.5 lines
    h = pw.History(tmp_path / "state", 600.0, epoch=lambda: BASE)
    assert h.start()
    try:
        for i in range(7):
            h.record(rec(BASE + i * 600))
        assert len(ts(raw(h.sdir))) == 7
        h.record(rec(BASE + 7 * 600))  # 8th line > 7.5: keep t > now - 3600
        assert ts(raw(h.sdir)) == [BASE + i * 600 for i in range(2, 8)]
        assert h.count["raw"] == 6
    finally:
        h.close()


def test_overfull_file_is_not_compacted_on_every_sample(tmp_path, monkeypatch):
    # raw recorded at a shorter interval: 24 lines inside the hour, but at 600 s the retention
    # is 6 points, so compaction cannot get under 1.25 x 6; it must not rewrite every sample
    sdir = tmp_path / "state"
    sdir.mkdir()
    write_points(raw(sdir), [{"t": BASE - 3500 + i * 100} for i in range(24)])
    compactions = []
    real = pw.compact_file
    monkeypatch.setattr(pw, "compact_file", lambda path, *a: (
        compactions.append(Path(path).name), real(path, *a))[1])
    h = pw.History(sdir, 600.0, epoch=lambda: BASE)
    assert h.start()
    try:
        compactions.clear()
        for i in range(1, 11):
            h.record(rec(BASE + i))
        assert len(ts(raw(sdir))) == 34  # nothing aged out yet
        # next threshold = kept + 0.25 x 6 points: one rewrite per 2 appends, not per append
        assert compactions.count("history-raw.jsonl") == 5
    finally:
        h.close()


def test_compaction_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "history-raw.jsonl"
    path.write_text(json.dumps({"t": 1.0}) + "\n" + json.dumps({"t": 5.0}) + "\n{torn")
    calls = []
    real_replace, real_fsync = os.replace, os.fsync
    monkeypatch.setattr(pw.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(pw.os, "replace",
                        lambda a, b: (calls.append((Path(a), Path(b))), real_replace(a, b)))
    assert pw.compact_file(path, keep_after=2.0) == (1, 5.0)
    assert calls[0] == "fsync"
    tmp, final = calls[1]
    assert final == path and tmp.parent == path.parent and tmp != path
    assert path.read_text() == '{"t": 5.0}\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["history-raw.jsonl"]


def test_failed_compaction_keeps_the_original(tmp_path, monkeypatch, capsys):
    sdir = tmp_path / "state"
    sdir.mkdir()
    original = json.dumps({"t": 1.0}) + "\n"
    raw(sdir).write_text(original)

    def boom(a, b):
        raise OSError("disk full")
    monkeypatch.setattr(pw.os, "replace", boom)
    h = pw.History(sdir, 2.0, epoch=lambda: BASE)
    assert h.start()
    h.close()
    assert raw(sdir).read_text() == original
    assert "cannot compact" in capsys.readouterr().err


def test_stop_during_append_does_not_duplicate_on_flush(history, monkeypatch):
    """SIGTERM (Stop) right after a bucket's line is written: flush() must not write it again."""
    sdir = history.sdir
    history.record(rec(BASE))
    real_write = pw.os.write

    def write_then_stop(fd, data):
        n = real_write(fd, data)
        if os.readlink(f"/proc/self/fd/{fd}").endswith("history-1m.jsonl"):
            raise pw.Stop()
        return n
    monkeypatch.setattr(pw.os, "write", write_then_stop)
    with pytest.raises(pw.Stop):
        history.record(rec(BASE + 60))  # closes minute BASE; Stop arrives mid-append
    monkeypatch.setattr(pw.os, "write", real_write)
    history.flush()
    assert ts(m1(sdir)) == [BASE]
    assert ts(m15(sdir)) == [BASE]


# ---------------------------------------------------------------- single writer

def test_second_writer_does_not_write(tmp_path, capsys):
    sdir = tmp_path / "state"
    first = pw.History(sdir, 2.0, epoch=lambda: BASE)
    second = pw.History(sdir, 2.0, epoch=lambda: BASE)
    assert first.start() is True
    try:
        assert second.start() is False
        second.record(rec(BASE))
        second.flush()
        assert not (sdir / "current.json").exists() and not raw(sdir).exists()
        assert capsys.readouterr().err.count("held by another power-watch --quiet") == 1
        first.record(rec(BASE))
        assert ts(raw(sdir)) == [BASE]
    finally:
        first.close()
    third = pw.History(sdir, 2.0, epoch=lambda: BASE + 10)
    assert third.start() is True  # the lock is released on close
    third.close()


def quiet_env(monkeypatch, root, tmp_path, notify=None):
    monkeypatch.setenv("POWER_WATCH_SYSFS", str(root))
    monkeypatch.setenv("POWER_WATCH_NVIDIA_SMI", fake_smi(tmp_path)[0])
    monkeypatch.setenv("POWER_WATCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("POWER_WATCH_NOTIFY", notify or str(tmp_path / "no-notify"))


def run_quiet(tmp_path, monkeypatch, root, ticks, argv=("--quiet",), notify=None):
    """Run main --quiet for `ticks` samples 2 s apart with an injected wall clock."""
    quiet_env(monkeypatch, root, tmp_path, notify)
    clock = FakeClock()
    start = clock.t
    epoch = lambda: BASE + (clock.t - start)

    def hook(t, dt):
        add_energy(root, 10_000_000 * dt)
        if t - start > ticks * 2:
            raise pw.Stop()
    clock.hooks.append(hook)
    return pw.main(list(argv), clock=clock, sleep=clock.sleep, epoch=epoch)


def test_history_failure_never_stops_alerting(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys")
    notify, log = fake_notify(tmp_path)

    def boom(self, rec):
        raise RuntimeError("history broke")
    monkeypatch.setattr(pw.History, "record", boom)
    rc = run_quiet(tmp_path, monkeypatch, root, ticks=10,
                   argv=("--quiet", "--threshold", "20", "--sustain", "0", "--alert-every", "4"),
                   notify=notify[0])
    assert rc == 0  # ran every tick until Stop
    assert len(log.read_text().splitlines()) >= 3  # alerts kept coming after the failures
    err = capsys.readouterr().err
    assert err.count("recording history failed (RuntimeError: history broke)") == 1


def test_stop_inside_history_still_stops_the_service(tmp_path, monkeypatch):
    root = make_sysfs(tmp_path / "sys")
    seen = []

    def stop(self, rec):
        seen.append(rec["epoch"])
        raise pw.Stop()  # SIGTERM landing inside record(): not swallowed by the catch-all
    monkeypatch.setattr(pw.History, "record", stop)
    assert run_quiet(tmp_path, monkeypatch, root, ticks=10) == 0
    assert seen == [BASE + 2]


def test_quiet_second_instance_keeps_running_without_writing(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys")
    sdir = tmp_path / "state"
    sdir.mkdir()
    fd = os.open(sdir / "history.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert run_quiet(tmp_path, monkeypatch, root, ticks=5) == 0
    finally:
        os.close(fd)
    assert sorted(p.name for p in sdir.iterdir()) == ["history.lock"]
    assert capsys.readouterr().err.count("held by another power-watch --quiet") == 1


# ----------------------------------------------------------------- current.json

def test_quiet_writes_current_json_and_history(tmp_path, monkeypatch):
    root = make_sysfs(tmp_path / "sys")
    write(root / "class/powercap/intel-rapl:0/constraint_0_power_limit_uw", 45_000_000)
    assert run_quiet(tmp_path, monkeypatch, root, ticks=40) == 0
    sdir = tmp_path / "state"
    current = json.loads((sdir / "current.json").read_text())
    assert list(current) == pw.REPORT_KEYS + ["interval_s", "cpu_limit_w", "cpu_cap", "cpu_cap_w",
                                              "gpu_max_limit_w", "epoch"]
    assert current["interval_s"] == 2.0 and current["cpu_limit_w"] == 45.0
    assert current["cpu_cap"] == "uncapped" and current["cpu_cap_w"] is None
    assert current["gpu_max_limit_w"] == 190.0 and current["epoch"] == BASE + 80
    assert current["cpu_w"] == 10.0 and current["total_kind"] == "estimated"
    assert isinstance(current["time"], str) and current["errors"] == []
    assert not (sdir / "current.json.tmp").exists()
    assert ts(raw(sdir)) == [BASE + 2 * i for i in range(1, 41)]
    assert ts(m1(sdir)) == [BASE, BASE + 60]  # the second minute flushed on Stop
    assert [p["n"] for p in lines(m1(sdir))] == [29, 11]
    assert ts(m15(sdir)) == [BASE] and lines(m15(sdir))[0]["n"] == 40


def test_current_json_cpu_limit_null_when_unreadable(tmp_path, monkeypatch):
    root = make_sysfs(tmp_path / "sys")
    assert run_quiet(tmp_path, monkeypatch, root, ticks=2) == 0
    assert json.loads((tmp_path / "state" / "current.json").read_text())["cpu_limit_w"] is None


def test_live_view_writes_no_history(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys")
    assert run_quiet(tmp_path, monkeypatch, root, ticks=3, argv=()) == 0
    sdir = tmp_path / "state"
    assert not (sdir / "current.json").exists() and not raw(sdir).exists()


def test_once_json_is_unchanged_and_writes_nothing(tmp_path):
    root = make_sysfs(tmp_path / "sys")
    write(root / "class/powercap/intel-rapl:0/constraint_0_power_limit_uw", 45_000_000)
    p = subprocess.run([str(SCRIPT), "--once", "--json", "--window", "0.05"],
                       env=cli_env(tmp_path, root, fake_smi(tmp_path)),
                       capture_output=True, text=True, timeout=30, check=False)
    assert p.returncode == 0, p.stderr
    assert list(json.loads(p.stdout)) == pw.REPORT_KEYS
    assert not (tmp_path / "state").exists()


def test_gpu_query_keeps_max_limit(tmp_path):
    root = make_sysfs(tmp_path / "sys")
    assert pw.read_gpus(root, fake_smi(tmp_path)).max_limit_w == 190.0
    smi = fake_smi(tmp_path, output="00000000:01:00.0, 1.0, 2.0, 1.00, [N/A], P0")
    assert pw.read_gpus(root, smi).max_limit_w is None


# ------------------------------------------------------------------ history CLI

NOW = BASE + 10 * 86400


def point(t, avg, mx=None, n=1, k="e"):
    return {"t": t, "n": n, "avg": avg, "max": avg if mx is None else mx, "cpu": 1.0,
            "gpu": 2.0, "rest": 25.0, "chg": 0.0, "ac": 1.0, "k": k}


def write_points(path, points, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(p) + "\n" for p in points) + tail)


def history_json(capsys, rng=None):
    argv = ["history", "--json"] + (["--range", rng] if rng else [])
    assert pw.main(argv, epoch=lambda: NOW) == 0
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def hist_state(tmp_path, monkeypatch):
    sdir = tmp_path / "state"
    monkeypatch.setenv("POWER_WATCH_STATE_DIR", str(sdir))
    return sdir


def test_history_empty_state(hist_state, capsys):
    for rng, tier, step in (("1h", "raw", 2.0), ("24h", "1m", 60.0), ("7d", "15m", 900.0)):
        data = history_json(capsys, rng)
        assert data == {"range": rng, "tier": tier, "step_s": step,
                        "from": NOW - pw.RANGES[rng][1], "to": NOW, "points": [],
                        "stats": {"max": None, "avg": None, "current": None},
                        "threshold_w": 150.0}
    assert not hist_state.exists()  # readers never write


def test_history_default_range_is_1h_with_raw_step_from_current(hist_state, capsys):
    hist_state.mkdir()
    (hist_state / "current.json").write_text(json.dumps(
        {"total_w": 77.0, "epoch": NOW - 1, "interval_s": 5.0, "threshold_w": 140.0}))
    write_points(raw(hist_state), [point(NOW - 3601, 500.0), point(NOW - 10, 60.0, 70.0),
                                   point(NOW - 5, 80.0, 90.0)], tail='{"t": 1')
    data = history_json(capsys)
    assert data["range"] == "1h" and data["tier"] == "raw" and data["step_s"] == 5.0
    assert [p["t"] for p in data["points"]] == [NOW - 10, NOW - 5]  # old and torn dropped
    assert data["points"][0] == point(NOW - 10, 60.0, 70.0)
    assert data["stats"] == {"max": 90.0, "avg": 70.0, "current": 77.0}
    assert data["threshold_w"] == 140.0


def test_history_24h_weighted_average_and_gaps(hist_state, capsys):
    pts = [point(NOW - 86400 - 60, 999.0, n=30),       # older than the range
           point(NOW - 7200, 100.0, 120.0, n=30),
           point(NOW - 7140, 40.0, 50.0, n=10),
           point(NOW - 600, 50.0, 55.0, n=20)]         # 109 min gap before it: kept as is
    write_points(m1(hist_state), pts)
    data = history_json(capsys, "24h")
    assert data["tier"] == "1m" and data["step_s"] == 60.0
    assert data["points"] == pts[1:]  # no filler points inside the gap
    assert data["stats"]["max"] == 120.0
    assert data["stats"]["avg"] == round((100 * 30 + 40 * 10 + 50 * 20) / 60, 2)
    assert data["stats"]["current"] == 50.0  # no current.json: last point's avg


def test_history_7d_stale_current_falls_back_to_last_point(hist_state, capsys):
    hist_state.mkdir()
    (hist_state / "current.json").write_text(json.dumps(
        {"total_w": 300.0, "epoch": NOW - 7, "interval_s": 2.0}))  # older than 3 x 2 s
    pts = [point(NOW - 8 * 86400, 1.0), point(NOW - 3 * 86400, 90.0, 140.0, n=900, k="x"),
           point(NOW - 900, None, None, n=900, k="m")]
    write_points(m15(hist_state), pts)
    data = history_json(capsys, "7d")
    assert data["tier"] == "15m" and data["step_s"] == 900.0
    assert data["points"] == pts[1:]
    assert data["stats"] == {"max": 140.0, "avg": 90.0, "current": None}


def test_history_ignores_points_from_the_future(hist_state, capsys):
    write_points(raw(hist_state), [point(NOW - 10, 60.0), point(NOW, 70.0),
                                   point(NOW + 100, 900.0)])  # written while the clock was ahead
    data = history_json(capsys)
    assert [p["t"] for p in data["points"]] == [NOW - 10, NOW]
    assert data["stats"] == {"max": 70.0, "avg": 65.0, "current": 70.0}


def test_history_text_output(hist_state, capsys):
    write_points(raw(hist_state), [point(NOW - 4, 60.0, 70.0), point(NOW - 2, 80.0, 90.0)])
    assert pw.main(["history"], epoch=lambda: NOW) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Max") and "90.0 W" in out[0]
    assert out[1].startswith("Average") and "70.0 W" in out[1]
    assert out[2].startswith("Current") and "80.0 W" in out[2]
    assert "2 points" in out[3] and "1h" in out[3]


def test_history_cli_subprocess_empty_and_bad_range(tmp_path):
    env = dict(os.environ, POWER_WATCH_STATE_DIR=str(tmp_path / "state"))
    p = subprocess.run([str(SCRIPT), "history", "--json", "--range", "24h"], env=env,
                       capture_output=True, text=True, timeout=30, check=False)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["points"] == [] and data["stats"]["max"] is None and data["tier"] == "1m"
    p = subprocess.run([str(SCRIPT), "history", "--range", "2h"], env=env,
                       capture_output=True, text=True, timeout=30, check=False)
    assert p.returncode == 2
    p = subprocess.run([str(SCRIPT), "history"], env=env, capture_output=True, text=True,
                       timeout=30, check=False)
    assert p.returncode == 0 and "Max" in p.stdout and "0 points" in p.stdout
