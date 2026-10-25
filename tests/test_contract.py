"""Producer-side guard for the data contract the Omarchy bar widget reads.

The state dir resolution, current.json and the history-*.jsonl tiers are power-watch's
public interface, documented in README.md "Data contract (public interface)". The
omarchy-power-watch plugin (https://github.com/rsd/omarchy-power-watch) reads them; changing
what is pinned here — a file name, a key or field the plugin reads, its JSON type, the atomic
rewrite of current.json, the state dir precedence — is a breaking change for it. Additive
changes (a new key, a new field) are compatible and need no change here.

These tests drive the real producer (`power-watch --quiet`, in process, as test_history.py
does) and check what it writes. The exact history line schema, bucket values, retention and
compaction behaviour are pinned in test_history.py (SCHEMA, test_bucket_*, test_startup_*,
test_quiet_writes_current_json_and_history); not repeated here.
"""

import json
import math
import os
import re
from pathlib import Path

import pytest
from test_history import BASE, quiet_env
from test_power_watch import (
    FakeClock,
    add_energy,
    add_psys,
    fake_smi,
    make_sysfs,
    no_user_config,  # noqa: F401 -- autouse fixture: never read the real user config
    pw,
    write,
)

CURRENT = "current.json"
TIER_FILES = {"raw": "history-raw.jsonl", "1m": "history-1m.jsonl", "15m": "history-15m.jsonl"}
# The plugin's ranges: key -> (tier it reads, span in s, bucket step in s; None = raw samples).
PLUGIN_RANGES = {"1h": ("raw", 3600, None), "24h": ("1m", 86400, 60), "7d": ("15m", 604800, 900)}
CURRENT_MAX_BYTES = 16384  # the plugin does not read a bigger current.json
HISTORY_MAX_LINE_CHARS = 1024  # the plugin skips a longer history line (Model.js)

NUM, NUM_OR_NULL = {"number"}, {"number", "null"}
STR_OR_NULL = {"string", "null"}
# Every current.json key the plugin reads, with the JSON types the producer may write there;
# cpu_cap and cpu_cap_w were added later (a plugin that predates them ignores them).
CURRENT_KEYS = {
    "epoch": NUM, "interval_s": NUM, "threshold_w": NUM,
    "ac_online": {"boolean", "null"}, "battery_status": STR_OR_NULL,
    "battery_capacity": NUM_OR_NULL, "battery_w": NUM_OR_NULL,
    "cpu_w": NUM_OR_NULL, "gpu_w": NUM_OR_NULL, "rest_w": NUM,
    "rest_source": {"string"}, "charge_w": NUM, "total_w": NUM_OR_NULL,
    "total_kind": {"string"}, "total_source": {"string"},
    "incomplete": {"boolean"}, "total_lower_bound": {"boolean"}, "psys_w": NUM_OR_NULL,
    "adapter_v": NUM_OR_NULL, "adapter_current_a": NUM_OR_NULL, "adapter_rating_w": NUM_OR_NULL,
    "cpu_limit_w": NUM_OR_NULL, "gpu_limit_w": NUM_OR_NULL, "gpu_max_limit_w": NUM_OR_NULL,
    "cpu_cap": STR_OR_NULL, "cpu_cap_w": NUM_OR_NULL,
    "gpu_pstate": STR_OR_NULL, "gpu_status": {"string"}, "gpu_devices": {"array"},
    "errors": {"array"},
}
GPU_DEVICE_KEYS = {"pci": {"string"}, "vendor": {"string"}, "status": {"string"},
                   "power_w": NUM_OR_NULL}
# Enumerations the plugin matches exactly (anything else shows as "not reported").
ENUMS = {
    # the plugin words "default" as "run power-watch calibrate"; the others as "<x> calibration"
    "rest_source": {"default", "calibrate", "passive"},
    "total_kind": {"measured", "estimated"},
    "total_source": {"battery", "psys", "model"},
    "gpu_status": {"active", "suspended", "none", "disabled", "unknown"},
    # power-cap's CPU cap against the live limits; null = cannot tell
    "cpu_cap": {"capped", "uncapped", "reverted", None},
}
GPU_DEVICE_ENUMS = {"vendor": {"nvidia", "amd"}, "status": {"active", "suspended", "unknown"}}
PCI_RE = r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]"
# Every history line field the plugin reads.
HISTORY_FIELDS = {"t": NUM, "n": NUM, "avg": NUM_OR_NULL, "max": NUM_OR_NULL,
                  "cpu": NUM_OR_NULL, "gpu": NUM_OR_NULL, "rest": NUM_OR_NULL,
                  "chg": NUM_OR_NULL, "ac": NUM_OR_NULL, "k": {"string"}}


def json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        assert math.isfinite(value), value  # NaN/Infinity are not JSON
        return "number"
    return {str: "string", list: "array", dict: "object"}[type(value)]


def run_service(tmp_path, monkeypatch, root, ticks=40, argv=(), env=None, psys_w=None):
    """`power-watch --quiet` for `ticks` samples 2 s apart (test_history.run_quiet, with the
    environment overridable after quiet_env: None unsets a variable)."""
    quiet_env(monkeypatch, root, tmp_path)
    for name, value in (env or {}).items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    clock = FakeClock()
    start = clock.t

    def hook(t, dt):
        add_energy(root, 10_000_000 * dt)
        if psys_w is not None:
            add_energy(root, psys_w * 1_000_000 * dt, "intel-rapl:1")
        if t - start > ticks * 2:
            raise pw.Stop()
    clock.hooks.append(hook)
    return pw.main(["--quiet", *argv], clock=clock, sleep=clock.sleep,
                   epoch=lambda: BASE + (clock.t - start))


def scenario(name, tmp_path, monkeypatch):
    """The service on one kind of machine; returns its state dir. Together the scenarios make
    every nullable key the plugin reads come out non-null at least once."""
    base = tmp_path / name
    env, argv, psys_w = {}, (), None
    if name == "ac-estimated":  # dGPU, adapter configured, CPU limit readable and capped
        root = make_sysfs(base / "sys")
        write(root / "class/powercap/intel-rapl:0/constraint_0_power_limit_uw", 45_000_000)
        argv = ("--adapter-voltage", "19.5", "--adapter-rating", "330")
        env = {"POWER_CAP_STATE_DIR": str(cap_state(base, {"constraint_0_power_limit_uw":
                                                           45_000_000}))}
    elif name == "battery":
        root = make_sysfs(base / "sys", ac=0, status="Discharging", current_ua=2_000_000, gpus=())
    elif name == "psys":
        root = make_sysfs(base / "sys", status="Charging", current_ua=1_000_000, gpus=())
        add_psys(root)
        psys_w = 60.0
    elif name == "gpu-unreadable":  # an estimate with an unknown term: a lower bound
        root = make_sysfs(base / "sys")
        env = {"POWER_WATCH_NVIDIA_SMI": fake_smi(base, rc=1, name="nvidia-smi-fails")[0]}
    else:
        raise ValueError(name)
    assert run_service(base, monkeypatch, root, argv=argv, env=env, psys_w=psys_w) == 0
    return base / "state"


SCENARIOS = ["ac-estimated", "battery", "psys", "gpu-unreadable"]


def cap_state(base, entry=None, originals=False):
    """A power-cap state dir: applied.json recording `entry` for intel-rapl:0 (an empty entry:
    a record of no zones), originals.json."""
    d = base / "run-power-cap"
    d.mkdir(parents=True, exist_ok=True)
    if entry is not None:
        zones = {"intel-rapl:0": entry} if entry else {}
        (d / "applied.json").write_text(json.dumps({"zones": zones, "time": BASE}))
    if originals:
        (d / "originals.json").write_text("{}")
    return d


# power-cap state (applied PL1 in uW, 0 = a record of no zones, None = no applied.json;
# originals.json present) -> cpu_cap, cpu_cap_w
CPU_CAP_CASES = [
    ((45_000_000, True), ("capped", 45.0)),
    ((30_000_000, True), ("reverted", 30.0)),   # live PL1 is 45 W: something restored it
    ((None, False), ("uncapped", None)),
    ((0, True), ("uncapped", None)),            # power-cap holds no CPU limit
    ((None, True), (None, None)),               # only an older power-cap leaves this
]


@pytest.mark.parametrize("state, expected", CPU_CAP_CASES)
def test_cpu_cap_is_one_of_the_pinned_values(tmp_path, monkeypatch, state, expected):
    pl1, originals = state
    root = make_sysfs(tmp_path / "sys")
    write(root / "class/powercap/intel-rapl:0/constraint_0_power_limit_uw", 45_000_000)
    entry = None if pl1 is None else {"constraint_0_power_limit_uw": pl1} if pl1 else {}
    sdir = cap_state(tmp_path, entry, originals)
    assert run_service(tmp_path, monkeypatch, root, ticks=2,
                       env={"POWER_CAP_STATE_DIR": str(sdir)}) == 0
    current = json.loads((tmp_path / "state" / CURRENT).read_text())
    assert (current["cpu_cap"], current["cpu_cap_w"]) == expected
    assert current["cpu_cap"] in ENUMS["cpu_cap"]


# ----------------------------------------------------------------- files

def test_file_names(tmp_path, monkeypatch):
    sdir = scenario("ac-estimated", tmp_path, monkeypatch)
    names = {p.name for p in sdir.iterdir()}
    assert {CURRENT, *TIER_FILES.values()} <= names
    assert pw.CURRENT_FILE == CURRENT
    assert {tier.name: tier.file for tier in pw.TIERS} == TIER_FILES


def test_tiers_cover_the_plugin_ranges():
    tiers = {tier.name: tier for tier in pw.TIERS}
    for key, (tier_name, span, step) in PLUGIN_RANGES.items():
        assert pw.RANGES[key] == (tier_name, float(span)), key  # same tier for the same range
        tier = tiers[tier_name]
        assert tier.step == step, key
        assert tier.retention >= span, key  # a range never shows less than it promises


# ------------------------------------------------------------ current.json

@pytest.fixture
def produced(tmp_path, monkeypatch):
    return {name: json.loads((scenario(name, tmp_path, monkeypatch) / CURRENT).read_text())
            for name in SCENARIOS}


def test_current_json_has_every_key_the_plugin_reads_with_its_type(produced):
    seen = {key: set() for key in CURRENT_KEYS}
    for name, current in produced.items():
        assert isinstance(current, dict), name
        for key, allowed in CURRENT_KEYS.items():
            assert key in current, f"{name}: {key} missing"
            kind = json_type(current[key])
            assert kind in allowed, f"{name}: {key} is {kind}, the plugin expects {allowed}"
            seen[key].add(kind)
    for key, allowed in CURRENT_KEYS.items():
        # a nullable key must still carry its real type somewhere, or the pin says nothing
        assert seen[key] - {"null"} == allowed - {"null"}, (key, seen[key])


def test_current_json_enums_and_gpu_devices(produced):
    devices = []
    for name, current in produced.items():
        for key, values in ENUMS.items():
            assert current[key] in values, (name, key, current[key])
        assert all(isinstance(e, str) for e in current["errors"]), name
        devices += current["gpu_devices"]
    assert devices, "no scenario reported a GPU device"
    for device in devices:
        assert json_type(device) == "object"
        for key, allowed in GPU_DEVICE_KEYS.items():
            assert json_type(device[key]) in allowed, (key, device)
        for key, values in GPU_DEVICE_ENUMS.items():
            assert device[key] in values, (key, device)
        assert re.fullmatch(PCI_RE, device["pci"]), device
    assert any(json_type(d["power_w"]) == "number" for d in devices)


# calibration.json content (None = no file) -> the rest_source the producer reports
REST_SOURCE_CASES = [
    (None, "default"),
    ({"rest_w": 30.0, "samples": 9, "source": "calibrate"}, "calibrate"),
    ({"rest_w": 30.0, "samples": 9, "source": "passive"}, "passive"),
    ({"rest_w": 30.0, "samples": 9, "source": "something-else"}, "calibrate"),
    ({"rest_w": 30.0, "samples": 9}, "calibrate"),
    ({"rest_w": -5, "source": "passive"}, "default"),  # implausible: ignored
    ("not json", "default"),
]


@pytest.mark.parametrize("calibration, expected", REST_SOURCE_CASES)
def test_rest_source_is_one_of_the_values_the_plugin_knows(tmp_path, monkeypatch, calibration,
                                                            expected):
    sdir = tmp_path / "state"
    if calibration is not None:
        sdir.mkdir()
        text = calibration if isinstance(calibration, str) else json.dumps(calibration)
        (sdir / "calibration.json").write_text(text)
    root = make_sysfs(tmp_path / "sys")  # on AC: no passive calibration rewrites the file
    assert run_service(tmp_path, monkeypatch, root, ticks=2) == 0
    current = json.loads((sdir / CURRENT).read_text())
    assert current["rest_source"] == expected
    assert current["rest_source"] in ENUMS["rest_source"]
    assert json_type(current["rest_w"]) == "number"


def test_current_json_fits_the_plugin_read_cap(tmp_path, monkeypatch):
    sdir = scenario("ac-estimated", tmp_path, monkeypatch)
    assert (sdir / CURRENT).stat().st_size < CURRENT_MAX_BYTES // 4


def test_current_json_is_replaced_atomically_every_sample(tmp_path, monkeypatch):
    """The plugin watches the state dir and reads current.json whole: every sample must land
    as a complete file renamed over the old one (a new inode), never written in place."""
    real_replace = os.replace
    replaced = []

    def replace(src, dst):
        src, dst = Path(src), Path(dst)
        if dst.name == CURRENT:
            assert src.parent == dst.parent and src.name != CURRENT
            data = json.loads(src.read_text())  # complete before it becomes current.json
            assert set(CURRENT_KEYS) <= set(data)
            before = dst.stat().st_ino if dst.exists() else None
            real_replace(src, dst)
            replaced.append((before, dst.stat().st_ino))
            return
        real_replace(src, dst)
    monkeypatch.setattr(pw.os, "replace", replace)
    root = make_sysfs(tmp_path / "sys")
    assert run_service(tmp_path, monkeypatch, root, ticks=5) == 0
    assert len(replaced) == 5  # one rewrite per sample
    assert all(before != after for before, after in replaced)


# ------------------------------------------------------------------ history

@pytest.mark.parametrize("name", SCENARIOS)
def test_history_lines_carry_the_fields_the_plugin_reads(tmp_path, monkeypatch, name):
    sdir = scenario(name, tmp_path, monkeypatch)
    for tier, file in TIER_FILES.items():
        text = (sdir / file).read_text()
        assert text.endswith("\n"), file  # whole lines only: the plugin parses from the end
        points = [json.loads(line) for line in text.splitlines()]
        assert points, file
        for point in points:
            assert json_type(point) == "object", file
            for field, allowed in HISTORY_FIELDS.items():
                assert field in point, (file, field)
                assert json_type(point[field]) in allowed, (file, field, point)
            assert point["n"] >= 1 and point["n"] == int(point["n"]), (file, point)
            assert point["ac"] is None or 0 <= point["ac"] <= 1, (file, point)
            assert point["k"] in {"m", "e", "x"}, (file, point)
        t = [p["t"] for p in points]
        assert t == sorted(set(t)), file  # oldest first, newest last
        step = PLUGIN_RANGES[{"raw": "1h", "1m": "24h", "15m": "7d"}[tier]][2]
        if step:
            assert all(v % step == 0 for v in t), file  # bucket start


def test_history_lines_fit_the_plugin_line_cap(tmp_path, monkeypatch):
    """The plugin skips a history line longer than HISTORY_MAX_LINE_CHARS: every line the
    producer writes, and a worst case of its own Bucket.line, stays well under it."""
    for name in SCENARIOS:
        sdir = scenario(name, tmp_path, monkeypatch)
        for file in TIER_FILES.values():
            for line in (sdir / file).read_text().splitlines():
                assert len(line) < HISTORY_MAX_LINE_CHARS // 4, (name, file, line)
    # every value at the longest float repr json.dumps can emit, a huge sample count
    huge = -1.2345678901234567e+300
    bucket = pw.Bucket(-1.2345678901234567e+300)
    bucket.add({"total_w": huge, "cpu_w": huge, "gpu_w": huge, "rest_w": huge,
                "charge_w": huge, "ac_online": True, "total_kind": "measured"})
    bucket.add({"total_kind": "estimated"})
    bucket.n = 10**18
    line = json.dumps(bucket.line())
    assert set(json.loads(line)) >= set(HISTORY_FIELDS)
    assert line.isascii()  # the plugin counts UTF-16 units; ASCII makes chars == bytes
    assert len(line) < HISTORY_MAX_LINE_CHARS // 2, line


# ---------------------------------------------------------------- state dir

STATE_DIR_CASES = [
    # (POWER_WATCH_STATE_DIR, XDG_STATE_HOME, expected); None = unset
    ("", "", "/home/u/.local/state/power-watch"),
    (None, None, "/home/u/.local/state/power-watch"),
    (None, "/x", "/x/power-watch"),
    ("", "/x/", "/x/power-watch"),
    ("", "/", "/power-watch"),
    ("/tmp/pw/", "/x", "/tmp/pw"),
    ("/", "/x", "/"),
    (" /tmp/pw", "/x", " /tmp/pw"),  # no trimming: a space is part of the path
]


@pytest.mark.parametrize("env_dir, xdg, expected", STATE_DIR_CASES)
def test_state_dir_precedence(monkeypatch, env_dir, xdg, expected):
    """POWER_WATCH_STATE_DIR > XDG_STATE_HOME/power-watch > ~/.local/state/power-watch; empty
    means unset; trailing slashes go, "/" stays "/"; nothing is trimmed. The plugin mirrors this
    resolution in the shell's environment."""
    monkeypatch.setenv("HOME", "/home/u")
    for name, value in (("POWER_WATCH_STATE_DIR", env_dir), ("XDG_STATE_HOME", xdg)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert str(pw.state_dir()) == expected


@pytest.mark.parametrize("where", ["xdg", "home"])
def test_service_writes_where_state_dir_points(tmp_path, monkeypatch, where):
    root = make_sysfs(tmp_path / "sys")
    home = tmp_path / "home"
    if where == "xdg":
        env = {"POWER_WATCH_STATE_DIR": None, "XDG_STATE_HOME": str(tmp_path / "xdg"),
               "HOME": str(home)}
        sdir = tmp_path / "xdg" / "power-watch"
    else:
        env = {"POWER_WATCH_STATE_DIR": "", "XDG_STATE_HOME": None, "HOME": str(home)}
        sdir = home / ".local" / "state" / "power-watch"
    assert run_service(tmp_path, monkeypatch, root, ticks=2, env=env) == 0
    assert (sdir / CURRENT).is_file() and (sdir / TIER_FILES["raw"]).is_file()
    assert not (tmp_path / "state").exists()
