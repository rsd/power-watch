"""Tests for bin/power-watch. Self-contained: builds a fake sysfs tree in tmp_path."""

import csv
import dataclasses
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "power-watch"
MAX_RANGE = 262143328850
GPU_PCI = "0000:01:00.0"
SMI_OK = "00000000:01:00.0, 36.70, 190.00, 1.00, 190.00, P0"
VENDORS = {"nvidia": "0x10de", "amd": "0x1002", "intel": "0x8086"}


def load_module():
    loader = SourceFileLoader("power_watch", str(SCRIPT))
    spec = importlib.util.spec_from_loader("power_watch", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["power_watch"] = module  # dataclasses look the module up here
    loader.exec_module(module)
    return module


pw = load_module()


# ------------------------------------------------------------------ fixtures

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n")


def make_sysfs(root, *, ac=1, status="Not charging", capacity=80, current_ua=0,
               voltage_uv=16_000_000, packages=(0,), subzones=True, gpus=(GPU_PCI,)):
    """Fake sysfs: AC, BAT0, RAPL package zones and (by default) one active NVIDIA dGPU."""
    supply = root / "class" / "power_supply"
    write(supply / "AC" / "online", ac)
    write(supply / "AC" / "type", "Mains")
    bat = supply / "BAT0"
    write(bat / "status", status)
    write(bat / "capacity", capacity)
    write(bat / "current_now", current_ua)
    write(bat / "voltage_now", voltage_uv)
    powercap = root / "class" / "powercap"
    (powercap / "intel-rapl").mkdir(parents=True, exist_ok=True)  # control type dir, no name
    for n in packages:
        zone = powercap / f"intel-rapl:{n}"
        write(zone / "name", f"package-{n}")
        write(zone / "enabled", 1)
        write(zone / "energy_uj", 1_000_000)
        write(zone / "max_energy_range_uj", MAX_RANGE)
        if subzones:
            for i, name in enumerate(("core", "uncore")):
                write(powercap / f"intel-rapl:{n}:{i}" / "name", name)
                write(powercap / f"intel-rapl:{n}:{i}" / "energy_uj", 5_000_000)
                write(powercap / f"intel-rapl:{n}:{i}" / "max_energy_range_uj", MAX_RANGE)
    for pci in gpus:
        add_gpu(root, pci)
    return root


def add_gpu(root, pci=GPU_PCI, vendor="nvidia", runtime="active", cls="0x030000", hwmon=None):
    """A PCI display device; `hwmon` = {attribute: value} under its hwmon/hwmon3 dir."""
    dev = root / "bus" / "pci" / "devices" / pci
    write(dev / "class", cls)
    write(dev / "vendor", VENDORS[vendor])
    if runtime is not None:
        write(dev / "power" / "runtime_status", runtime)
    for name, value in (hwmon or {}).items():
        write(dev / "hwmon" / "hwmon3" / name, value)
    return dev


def add_psys(root, n=1):
    zone = root / "class" / "powercap" / f"intel-rapl:{n}"
    write(zone / "name", "psys")
    write(zone / "energy_uj", 1_000_000)
    write(zone / "max_energy_range_uj", MAX_RANGE)
    return zone


def energy_path(root, zone="intel-rapl:0"):
    return root / "class" / "powercap" / zone / "energy_uj"


def add_energy(root, uj, zone="intel-rapl:0"):
    path = energy_path(root, zone)
    path.write_text(f"{int(path.read_text()) + int(uj)}\n")


def set_supply(root, *, ac=None, status=None, current_ua=None):
    supply = root / "class" / "power_supply"
    if ac is not None:
        write(supply / "AC" / "online", ac)
    if status is not None:
        write(supply / "BAT0" / "status", status)
    if current_ua is not None:
        write(supply / "BAT0" / "current_now", current_ua)


def fake_smi(tmp_path, output=SMI_OK, rc=0, name="nvidia-smi"):
    """Fake nvidia-smi printing `output` (a line or a list of lines); each call appends its
    arguments to <name>.calls (see smi_calls)."""
    path = tmp_path / name
    lines = [output] if isinstance(output, str) else list(output)
    prints = "".join(f"printf '%s\\n' '{line}'\n" for line in lines)
    path.write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path / (name + ".calls")}"\n{prints}exit {rc}\n')
    path.chmod(0o755)
    return [str(path)]


def smi_calls(tmp_path, name="nvidia-smi"):
    """Argument lines of every fake nvidia-smi call so far ([] = never invoked)."""
    calls = tmp_path / f"{name}.calls"
    return calls.read_text().splitlines() if calls.exists() else []


def fake_notify(tmp_path, rc=0):
    log = tmp_path / "notify.log"
    path = tmp_path / "notify-send"
    path.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> "{log}"\necho >> "{log}"\nexit {rc}\n')
    path.chmod(0o755)
    return [str(path)], log


class FakeClock:
    """Monotonic clock + sleep; sleeping advances time and runs hooks (e.g. RAPL energy)."""

    def __init__(self, start=1000.0):
        self.t = start
        self.hooks = []

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt
        for hook in self.hooks:
            hook(self.t, dt)


@pytest.fixture(autouse=True)
def no_user_config(tmp_path, monkeypatch):
    """Never read the real ~/.config/power-watch/config, $POWER_WATCH_CONFIG or power-cap's
    /run/power-cap (an absent power-cap state dir: cpu_cap "uncapped")."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("POWER_WATCH_CONFIG", raising=False)
    monkeypatch.setenv("POWER_CAP_STATE_DIR", str(tmp_path / "no-power-cap"))


@pytest.fixture
def sysfs(tmp_path):
    return make_sysfs(tmp_path / "sys")


# The owner's values (the old built-in constants), for the model/render tests.
CFG = pw.Config(adapter_v=19.5, rating_w=330.0, threshold_w=150.0)


def sample(**kw):
    defaults = {"mono": 0.0, "time": "t", "ac_online": True, "battery_status": "Not charging",
                "battery_capacity": 80, "battery_w": 0.0, "cpu_w": 10.0,
                "gpu": pw.Gpu(36.7, 190.0, "P0")}
    defaults.update(kw)
    return pw.Sample(**defaults)


def report(total_w=200.0, kind="estimated", **kw):
    r = {"total_w": total_w, "total_kind": kind, "adapter_current_a": total_w / 19.5,
         "adapter_v": 19.5, "incomplete": False, "cpu_w": 80.0, "ac_online": True,
         "total_source": "battery" if kind == "measured" else "model",
         "total_lower_bound": False, "gpu_status": "active", "battery_status": "Not charging",
         "battery_w": 0.0}
    r.update(kw)
    return r


# ---------------------------------------------------------------------- RAPL

def test_rapl_delta():
    r1 = {"intel-rapl:0": (1_000_000, MAX_RANGE)}
    r2 = {"intel-rapl:0": (21_000_000, MAX_RANGE)}
    assert pw.rapl_watts(r1, r2, 2.0) == pytest.approx(10.0)


def test_rapl_wraparound():
    r1 = {"intel-rapl:0": (MAX_RANGE - 1_000_000, MAX_RANGE)}
    r2 = {"intel-rapl:0": (3_000_000, MAX_RANGE)}
    assert pw.rapl_watts(r1, r2, 1.0) == pytest.approx(4.0)


def test_rapl_zero_dt_is_unknown():
    r = {"intel-rapl:0": (1, MAX_RANGE)}
    assert pw.rapl_watts(r, r, 0.0) is None


def test_multi_package_sum_ignores_subzones(tmp_path):
    root = make_sysfs(tmp_path / "sys", packages=(0, 1))
    assert [z.name for z in pw.rapl_zones(root)] == ["intel-rapl:0", "intel-rapl:1"]
    meter = pw.RaplMeter(root)
    assert meter.sample(10.0) == (None, None)  # first read only primes
    add_energy(root, 5_000_000, "intel-rapl:0")
    add_energy(root, 15_000_000, "intel-rapl:1")
    add_energy(root, 900_000_000, "intel-rapl:0:0")  # subzone must not count
    watts, err = meter.sample(11.0)
    assert err is None and watts == pytest.approx(20.0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_rapl_permission_error_gives_fix_hint(sysfs):
    energy_path(sysfs).chmod(0o000)
    try:
        with pytest.raises(pw.RaplError) as exc:
            pw.read_rapl(sysfs)
    finally:
        energy_path(sysfs).chmod(0o644)
    msg = str(exc.value)
    assert "root-only" in msg and "0400" in msg
    assert "sudo ./install.sh" in msg
    assert "sudo systemd-tmpfiles --create /etc/tmpfiles.d/power-watch.conf" in msg


def test_rapl_absent_gives_fix_hint(tmp_path):
    with pytest.raises(pw.RaplError, match="sudo ./install.sh"):
        pw.read_rapl(tmp_path)


# ----------------------------------------------------------------------- GPU

def read_gpus(tmp_path, output=SMI_OK, rc=0, smi=None, **kw):
    root = tmp_path / "sys"
    if not root.exists():
        make_sysfs(root)
    return pw.read_gpus(root, smi or fake_smi(tmp_path, output, rc), **kw)


def test_gpu_ok(tmp_path):
    gpu = read_gpus(tmp_path)
    assert (gpu.power_w, gpu.limit_w, gpu.pstate, gpu.error) == (36.7, 190.0, "P0", None)
    assert gpu.state == "active" and gpu.max_limit_w == 190.0
    assert [d.summary() for d in gpu.devices] == [
        {"pci": GPU_PCI, "vendor": "nvidia", "status": "active", "power_w": 36.7}]
    assert smi_calls(tmp_path) == [
        f"--query-gpu={pw.GPU_QUERY} --format=csv,noheader,nounits"]  # all NVIDIA GPUs: no -i


def test_gpu_missing_binary(tmp_path):
    gpu = read_gpus(tmp_path, smi=[str(tmp_path / "no-such-smi")])
    assert gpu.power_w is None and "not found" in gpu.error and gpu.state == "unknown"


def test_gpu_nonzero_exit(tmp_path):
    gpu = read_gpus(tmp_path, output="NVIDIA-SMI has failed", rc=9)
    assert gpu.power_w is None and "exited 9" in gpu.error


def test_gpu_na_fields(tmp_path):
    gpu = read_gpus(tmp_path, output="00000000:01:00.0, [N/A], [N/A], 1.00, 190.00, [N/A]")
    assert (gpu.power_w, gpu.limit_w, gpu.pstate) == (None, None, None)
    assert "[N/A]" in gpu.error and gpu.state == "unknown"


def test_gpu_only_limit_na(tmp_path):
    gpu = read_gpus(tmp_path, output="00000000:01:00.0, 40.10, [N/A], 1.00, 190.00, P2")
    assert (gpu.power_w, gpu.limit_w, gpu.pstate, gpu.error) == (40.1, None, "P2", None)


def test_gpu_unexpected_output_and_missing_row(tmp_path):
    gpu = read_gpus(tmp_path, output="garbage")
    assert gpu.state == "unknown" and "unexpected" in gpu.error
    (tmp_path / "other").mkdir()
    gpu = pw.read_gpus(tmp_path / "sys", fake_smi(tmp_path / "other",
                       output="00000000:02:00.0, 1.0, 2.0, 1.0, 2.0, P0"))
    assert gpu.state == "unknown" and "no row for 0000:01:00.0" in gpu.error


# ------------------------------------------------------------------- battery

def test_battery_watts_from_current_and_voltage(sysfs):
    set_supply(sysfs, current_ua=-2_500_000)
    assert pw.battery_watts(pw.battery_dirs(sysfs)[0]) == pytest.approx(40.0)


def test_battery_prefers_power_now(sysfs):
    bat = pw.battery_dirs(sysfs)[0]
    write(bat / "power_now", 12_345_000)
    set_supply(sysfs, current_ua=9_000_000)
    assert pw.battery_watts(bat) == pytest.approx(12.345)


def test_battery_not_named_bat0(tmp_path):
    write(tmp_path / "class/power_supply/BAT1/status", "Full")
    write(tmp_path / "class/power_supply/BAT1/capacity", 97)
    assert pw.read_batteries(tmp_path) == ("Full", 97, None)
    assert pw.read_batteries(tmp_path / "nothing") is None


# --------------------------------------------------------------------- model

def test_model_on_battery_is_measured_with_rest_sample():
    s = sample(ac_online=False, battery_status="Discharging", battery_w=64.0)
    r = pw.model(s, pw.Calibration(), CFG)
    assert r["total_kind"] == "measured" and r["total_w"] == 64.0
    assert r["adapter_current_a"] is None and r["incomplete"] is False
    assert pw.rest_sample(r) == pytest.approx(64.0 - 10.0 - 36.7)


def test_model_on_battery_without_gpu_gives_no_rest_sample():
    s = sample(ac_online=False, battery_status="Discharging", battery_w=64.0, gpu=pw.Gpu())
    assert pw.rest_sample(pw.model(s, pw.Calibration(), CFG)) is None


def test_model_on_ac_default_rest():
    r = pw.model(sample(), pw.Calibration(), CFG)
    assert r["total_kind"] == "estimated" and r["rest_source"] == "default"
    assert r["total_w"] == pytest.approx(10.0 + 36.7 + 25.0)
    assert r["adapter_current_a"] == pytest.approx(round(71.7 / 19.5, 2))
    assert pw.rest_sample(r) is None
    assert "DEFAULT — run `power-watch calibrate`" in pw.render(r)


@pytest.mark.parametrize("source, label", [
    ("default", "DEFAULT — run `power-watch calibrate`"), ("calibrate", "calibrated"),
    ("passive", "passively calibrated"), ("something-new", "something-new")])
def test_render_rest_label_words_the_rest_source(source, label):
    r = pw.model(sample(), pw.Calibration(18.0, source), CFG)
    assert r["rest_source"] == source                          # the contract value is unchanged
    line = next(x for x in pw.render(r).splitlines() if x.startswith("Rest"))
    assert line.endswith(f"({label})")
    assert "calibrate calibration" not in pw.render(r)


def test_model_on_ac_charging_adds_battery_power():
    r = pw.model(sample(battery_status="Charging", battery_w=30.0), pw.Calibration(18.0, "calibrate"), CFG)
    assert r["charge_w"] == 30.0 and r["rest_source"] == "calibrate"
    assert r["total_w"] == pytest.approx(10.0 + 36.7 + 18.0 + 30.0)


def test_model_on_ac_not_charging_adds_zero():
    r = pw.model(sample(battery_status="Not charging", battery_w=30.0), pw.Calibration(), CFG)
    assert r["charge_w"] == 0.0


def test_model_incomplete_when_gpu_unknown():
    r = pw.model(sample(gpu=pw.Gpu(error="GPU power unknown")), pw.Calibration(), CFG)
    assert r["incomplete"] is True and r["gpu_w"] is None and r["gpu_status"] == "unknown"
    assert r["total_w"] == pytest.approx(35.0) and r["total_lower_bound"] is True
    out = pw.render(r)
    assert "≥  35.0 W" in out and "(lower bound: unknown terms counted as 0)" in out


# --------------------------------------------------------------- calibration

def test_load_calibration_default_and_file(tmp_path):
    assert pw.load_calibration(tmp_path).source == "default"
    pw.save_calibration(tmp_path, 18.4, 12, "calibrate")
    cal = pw.load_calibration(tmp_path)
    assert (cal.rest_w, cal.samples, cal.source) == (18.4, 12, "calibrate")


def test_load_calibration_rejects_garbage(tmp_path):
    (tmp_path / "calibration.json").write_text("{not json")
    cal = pw.load_calibration(tmp_path)
    assert cal.source == "default" and cal.error


def test_save_calibration_is_atomic(tmp_path, monkeypatch):
    sdir = tmp_path / "state" / "power-watch"
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(pw.os, "replace", lambda a, b: (calls.append((Path(a), Path(b))), real_replace(a, b)))
    pw.save_calibration(sdir, 20.0, 10, "passive")
    pw.save_calibration(sdir, 21.0, 11, "passive")
    assert len(calls) == 2
    tmp, final = calls[0]
    assert final == sdir / "calibration.json" and tmp.parent == sdir and tmp != final
    assert sorted(p.name for p in sdir.iterdir()) == ["calibration.json"]
    data = json.loads(final.read_text())
    assert data["rest_w"] == 21.0 and data["samples"] == 11 and data["source"] == "passive"
    assert "updated" in data


def test_passive_calibration_median_and_rate_limit(tmp_path):
    passive = pw.PassiveCalibrator(tmp_path)
    for i in range(9):
        assert passive.feed(20.0 + i, now=i) is None
    assert not (tmp_path / "calibration.json").exists()
    assert passive.feed(29.0, now=9) == pytest.approx(24.5)
    assert json.loads((tmp_path / "calibration.json").read_text())["samples"] == 10
    assert passive.feed(30.0, now=20) is None  # < 30 s since the last write
    assert passive.feed(31.0, now=39) == pytest.approx(25.5)
    assert json.loads((tmp_path / "calibration.json").read_text())["samples"] == 12


def test_passive_window_is_bounded(tmp_path):
    passive = pw.PassiveCalibrator(tmp_path)
    for i in range(40):
        passive.feed(10.0 if i < 10 else 50.0, now=0)
    assert len(passive.window) == 30 and set(passive.window) == {50.0}


def test_passive_rejects_implausible(tmp_path):
    passive = pw.PassiveCalibrator(tmp_path)
    for value in (-5.0, 250.0, None):
        passive.feed(value, now=0)
    assert len(passive.window) == 0


def test_monitor_feeds_passive_only_when_discharging(tmp_path):
    root = make_sysfs(tmp_path / "sys", ac=0, status="Discharging", current_ua=4_000_000)
    clock = FakeClock()
    clock.hooks.append(lambda t, dt: add_energy(root, 10_000_000 * dt))
    sampler = pw.Sampler(root, fake_smi(tmp_path), clock=clock, sleep=clock.sleep)
    monitor = pw.Monitor(sampler, tmp_path / "state", CFG, passive=pw.PassiveCalibrator(tmp_path / "state"))
    sampler.prime()
    for _ in range(10):
        clock.sleep(2)
        r = monitor.tick()
    assert r["total_kind"] == "measured" and r["cpu_w"] == pytest.approx(10.0)
    cal = pw.load_calibration(tmp_path / "state")
    assert cal.source == "passive" and cal.rest_w == pytest.approx(64.0 - 10.0 - 36.7)


# ----------------------------------------------------------- calibrate command

def run_main(argv, clock, monkeypatch, root, tmp_path, smi=None):
    monkeypatch.setenv("POWER_WATCH_SYSFS", str(root))
    monkeypatch.setenv("POWER_WATCH_NVIDIA_SMI", (smi or fake_smi(tmp_path))[0])
    monkeypatch.setenv("POWER_WATCH_STATE_DIR", str(tmp_path / "state"))
    return pw.main(argv, clock=clock, sleep=clock.sleep)


def test_calibrate_waits_for_unplug_then_saves(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys", ac=1, status="Not charging", current_ua=0)
    clock = FakeClock()
    start = clock.t
    clock.hooks.append(lambda t, dt: add_energy(root, 10_000_000 * dt))

    def unplug(t, dt):
        if t - start >= 5:
            set_supply(root, ac=0, status="Discharging", current_ua=4_000_000)
    clock.hooks.append(unplug)

    rc = run_main(["calibrate", "--duration", "30"], clock, monkeypatch, root, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Unplug the power adapter now" in out
    assert "plug the power adapter back in" in out
    assert "25.0 W" in out  # the default it replaces
    cal = pw.load_calibration(tmp_path / "state")
    assert cal.source == "calibrate" and cal.samples == 15
    assert cal.rest_w == pytest.approx(64.0 - 10.0 - 36.7)


def test_calibrate_unplug_timeout(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys", ac=1)
    clock = FakeClock()
    rc = run_main(["calibrate"], clock, monkeypatch, root, tmp_path)
    assert rc == 1
    assert "still plugged in" in capsys.readouterr().err
    assert not (tmp_path / "state" / "calibration.json").exists()


def test_calibrate_insufficient_samples(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys", ac=0, status="Discharging", current_ua=4_000_000)
    clock = FakeClock()
    clock.hooks.append(lambda t, dt: add_energy(root, 10_000_000 * dt))
    rc = run_main(["calibrate", "--duration", "10"], clock, monkeypatch, root, tmp_path)
    assert rc == 1
    assert "need 10" in capsys.readouterr().err
    assert not (tmp_path / "state" / "calibration.json").exists()


def test_calibrate_rejects_samples_without_gpu(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys", ac=0, status="Discharging", current_ua=4_000_000)
    clock = FakeClock()
    clock.hooks.append(lambda t, dt: add_energy(root, 10_000_000 * dt))
    smi = fake_smi(tmp_path, rc=1)
    rc = run_main(["calibrate", "--duration", "40"], clock, monkeypatch, root, tmp_path, smi=smi)
    captured = capsys.readouterr()
    assert rc == 1 and "GPU unknown" in captured.out


# ------------------------------------------------------------------- alerter

def test_alerter_sustain_rate_limit_and_reset(tmp_path):
    notify, log = fake_notify(tmp_path)
    a = pw.Alerter(threshold=150, sustain=10, every=300, notify=notify)
    assert a.update(report(), 0) is False
    assert a.update(report(), 5) is False
    assert a.update(report(), 10) is True
    assert a.update(report(), 12) is False      # rate limited
    assert a.update(report(), 310) is True      # alert-every elapsed
    assert a.update(report(120.0), 320) is False and a.above_since is None  # reset
    assert a.update(report(), 321) is False
    assert a.update(report(), 330) is False     # sustain restarted at 321
    assert a.update(report(), 611) is True
    lines = log.read_text().splitlines()
    assert len(lines) == 3
    args = lines[0].split("|")
    assert args[:2] == ["-u", "critical"]
    assert "200 W" in args[3] and "19.5 V" in args[3] and "150 W" in args[3]
    assert "sudo power-cap set --safe" in args[3]


def test_alerter_never_on_battery(tmp_path):
    notify, log = fake_notify(tmp_path)
    a = pw.Alerter(threshold=150, sustain=0, every=0, notify=notify)
    for t in range(5):
        assert a.update(report(250.0, kind="measured", adapter_current_a=None, ac_online=False),
                        t) is False
    assert not log.exists()


def test_alerter_missing_notify_warns_once(tmp_path, capsys):
    a = pw.Alerter(threshold=150, sustain=0, every=0, notify=[str(tmp_path / "no-notify")])
    assert a.update(report(), 0) is True
    assert a.update(report(), 1) is True
    err = capsys.readouterr().err
    assert err.count("notification failed") == 1


def test_alerter_failing_notify_does_not_crash(tmp_path, capsys):
    notify, _ = fake_notify(tmp_path, rc=1)
    a = pw.Alerter(threshold=150, sustain=0, every=0, notify=notify)
    assert a.update(report(), 0) is True
    assert "exited 1" in capsys.readouterr().err


def test_alerter_silent_while_cpu_unknown(tmp_path):
    notify, log = fake_notify(tmp_path)
    a = pw.Alerter(threshold=150, sustain=0, every=0, notify=notify)
    assert a.update(report(cpu_w=None, incomplete=True), 0) is False
    assert a.above_since is None and not log.exists()
    assert a.update(report(), 1) is True


def test_rapl_alarm_rate_limit_and_recovery(tmp_path, capsys):
    notify, log = fake_notify(tmp_path)
    alarm = pw.RaplAlarm(every=60, notify=notify)
    assert alarm.update("Cannot read energy_uj. Fix: sudo ./install.sh", 0) is True
    assert alarm.update("still", 59) is False
    assert alarm.update("still", 60) is True
    assert alarm.update(None, 62) is False
    assert alarm.update(None, 64) is False
    assert alarm.update("again", 66) is True     # a new outage notifies at once
    lines = log.read_text().splitlines()
    assert len(lines) == 3
    args = lines[0].split("|")
    assert args[:3] == ["-u", "critical", "power-watch: CPU power unreadable"]
    assert "sudo systemd-tmpfiles --create /etc/tmpfiles.d/power-watch.conf" in args[3]
    err = capsys.readouterr().err
    assert err.count("RAPL readable again") == 1 and "sudo ./install.sh" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_quiet_rapl_unreadable_notifies_keeps_running_and_recovers(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys")
    # rest 125 W: GPU 36.7 + rest is already above 150 W without the CPU term, so a
    # high-load alert during the outage would show the CPU-unknown suppression is missing
    pw.save_calibration(tmp_path / "state", 125.0, 10, "calibrate")
    notify, log = fake_notify(tmp_path)
    monkeypatch.setenv("POWER_WATCH_NOTIFY", notify[0])
    count = lambda: len(log.read_text().splitlines()) if log.exists() else 0
    clock = FakeClock()
    start, recover_at, seen = clock.t, clock.t + 70, {}
    energy_path(root).chmod(0o000)

    def timeline(t, dt):
        if t - start == 60:
            seen["before_window"] = count()  # runs before the tick at +60 s
        if t >= recover_at:
            energy_path(root).chmod(0o644)
            add_energy(root, 10_000_000 * dt)
        if t - start >= 100:
            raise pw.Stop()
    clock.hooks.append(timeline)
    try:
        rc = run_main(["--quiet", "--alert-every", "60", "--sustain", "10"],
                      clock, monkeypatch, root, tmp_path)
    finally:
        energy_path(root).chmod(0o644)
    assert rc == 0  # stopped by the injected Stop, never exited on its own
    lines = log.read_text().splitlines()
    titles = [line.split("|")[2] for line in lines]
    assert seen["before_window"] == 1
    assert titles[:2] == ["power-watch: CPU power unreadable"] * 2
    assert len(titles) == 3 and titles[2].startswith("Power adapter overload")
    err = capsys.readouterr().err
    assert err.count("RAPL readable again") == 1
    assert "sudo ./install.sh" in err and "root-only" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_live_rapl_unreadable_exits_1(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys")
    energy_path(root).chmod(0o000)
    try:
        rc = run_main([], FakeClock(), monkeypatch, root, tmp_path)
    finally:
        energy_path(root).chmod(0o644)
    assert rc == 1 and "sudo ./install.sh" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_live_rapl_unreadable_mid_run_exits_1(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys")
    clock = FakeClock()
    clock.hooks.append(lambda t, dt: energy_path(root).chmod(0o000) if t - 1000 >= 6 else None)
    try:
        rc = run_main([], clock, monkeypatch, root, tmp_path)
    finally:
        energy_path(root).chmod(0o644)
    assert rc == 1 and "root-only" in capsys.readouterr().err


# ----------------------------------------------------------------------- log

def test_csv_header_written_once(tmp_path):
    path = tmp_path / "log.csv"
    path.touch()  # existing but empty -> header
    r = pw.model(sample(), pw.Calibration(), CFG)
    pw.CsvLog(path).write(r)
    pw.CsvLog(path).write(r)
    rows = list(csv.reader(path.open()))
    assert rows[0] == pw.LOG_KEYS and "errors" not in rows[0]
    assert len(rows) == 3 and rows[1] == rows[2]
    assert rows[1][pw.LOG_KEYS.index("gpu_w")] == "36.7"


# ----------------------------------------------------------------------- CLI

def cli_env(tmp_path, root, smi):
    env = dict(os.environ, POWER_WATCH_SYSFS=str(root), POWER_WATCH_NVIDIA_SMI=smi[0],
               POWER_WATCH_STATE_DIR=str(tmp_path / "state"),
               POWER_WATCH_NOTIFY=str(tmp_path / "no-notify"),
               XDG_CONFIG_HOME=str(tmp_path / "xdg-config"))
    env.pop("POWER_WATCH_CONFIG", None)
    return env


def run_cli(args, env):
    return subprocess.run([str(SCRIPT), *args], env=env, check=False,
                          capture_output=True, text=True, timeout=30)


def test_once_json_ok(tmp_path, sysfs):
    p = run_cli(["--once", "--json", "--window", "0.05"], cli_env(tmp_path, sysfs, fake_smi(tmp_path)))
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert list(data) == pw.REPORT_KEYS
    assert data["cpu_w"] == 0.0 and data["gpu_w"] == 36.7 and data["errors"] == []
    assert data["total_kind"] == "estimated" and data["rest_source"] == "default"
    assert data["incomplete"] is False
    assert not (tmp_path / "state").exists()  # --once writes nothing


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_once_json_rapl_unreadable_exits_1(tmp_path, sysfs):
    energy_path(sysfs).chmod(0o000)
    try:
        p = run_cli(["--once", "--json", "--window", "0.05"],
                    cli_env(tmp_path, sysfs, fake_smi(tmp_path)))
    finally:
        energy_path(sysfs).chmod(0o644)
    assert p.returncode == 1
    data = json.loads(p.stdout)
    assert data["cpu_w"] is None and data["incomplete"] is True
    assert any("sudo ./install.sh" in e for e in data["errors"])


def test_quiet_without_rapl_notifies_and_keeps_running(tmp_path):
    root = make_sysfs(tmp_path / "sys", packages=())  # e.g. rapl module not loaded yet
    notify, log = fake_notify(tmp_path)
    env = dict(cli_env(tmp_path, root, fake_smi(tmp_path)), POWER_WATCH_NOTIFY=notify[0])
    proc = subprocess.Popen([str(SCRIPT), "--quiet", "--interval", "0.1"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not log.exists():
            time.sleep(0.05)
        time.sleep(0.3)  # several more samples: must neither exit nor re-notify
        assert proc.poll() is None
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    assert "sudo ./install.sh" in err
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and "power-watch: CPU power unreadable" in lines[0]


def test_quiet_logs_and_exits_0_on_sigterm(tmp_path, sysfs):
    log = tmp_path / "log.csv"
    proc = subprocess.Popen([str(SCRIPT), "--quiet", "--interval", "0.1", "--log", str(log)],
                            env=cli_env(tmp_path, sysfs, fake_smi(tmp_path)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if log.exists() and len(log.read_text().splitlines()) >= 3:
                break
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    assert out == ""
    rows = list(csv.reader(log.open()))
    assert rows[0] == pw.LOG_KEYS and len(rows) >= 3


def test_json_requires_once(tmp_path, sysfs):
    p = run_cli(["--json"], cli_env(tmp_path, sysfs, fake_smi(tmp_path)))
    assert p.returncode == 2


# -------------------------------------------------------------------- config

def write_config(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def run_once(tmp_path, monkeypatch, capsys, argv=(), root=None, smi=None):
    """main --once --json in-process (sysfs default: tmp_path/sys, made on first use).
    Returns (rc, report or None, stderr)."""
    if root is None:
        root = tmp_path / "sys" if (tmp_path / "sys").exists() else make_sysfs(tmp_path / "sys")
    rc = run_main(["--once", "--json", "--window", "0.05", *argv], FakeClock(), monkeypatch,
                  root, tmp_path, smi=smi)
    out, err = capsys.readouterr()
    return rc, (json.loads(out) if out.strip() else None), err


def test_config_defaults_without_a_file(tmp_path, monkeypatch, capsys):
    rc, r, err = run_once(tmp_path, monkeypatch, capsys)
    assert rc == 0 and err == ""
    assert (r["adapter_v"], r["adapter_current_a"], r["adapter_rating_w"]) == (None, None, None)
    assert r["threshold_w"] == 150.0 and r["rest_w"] == 25.0


def test_config_file_then_flag_precedence(tmp_path, monkeypatch, capsys):
    write_config(tmp_path / "xdg-config" / "power-watch" / "config",
                 "[adapter]\nvoltage_v = 20  # inline comment\nrating_w = 230\n"
                 "[alert]\nthreshold_w = 120\n[estimate]\nrest_w = 30\n"
                 "[power-cap]\nsafe_cpu_w = 45\n[someone-else]\nwhatever = 1\n")
    rc, r, err = run_once(tmp_path, monkeypatch, capsys)
    assert rc == 0 and err == ""  # [power-cap] and unknown sections are ignored silently
    assert (r["adapter_v"], r["adapter_rating_w"], r["threshold_w"], r["rest_w"]) == (20.0, 230.0, 120.0, 30.0)
    assert r["adapter_current_a"] == pytest.approx(round(r["total_w"] / 20.0, 2))
    rc, r, _ = run_once(tmp_path, monkeypatch, capsys,
                        ["--threshold", "130", "--adapter-voltage", "19", "--adapter-rating", "300",
                         "--rest", "10"])
    assert (r["adapter_v"], r["adapter_rating_w"], r["threshold_w"], r["rest_w"]) == (19.0, 300.0, 130.0, 10.0)


def test_config_env_and_flag_paths(tmp_path, monkeypatch, capsys):
    write_config(tmp_path / "xdg-config" / "power-watch" / "config", "[alert]\nthreshold_w = 101\n")
    env_file = write_config(tmp_path / "env.ini", "[alert]\nthreshold_w = 102\n")
    flag_file = write_config(tmp_path / "flag.ini", "[alert]\nthreshold_w = 103\n")
    assert run_once(tmp_path, monkeypatch, capsys)[1]["threshold_w"] == 101.0
    monkeypatch.setenv("POWER_WATCH_CONFIG", str(env_file))
    assert run_once(tmp_path, monkeypatch, capsys)[1]["threshold_w"] == 102.0
    assert run_once(tmp_path, monkeypatch, capsys, ["--config", str(flag_file)])[1]["threshold_w"] == 103.0


@pytest.mark.parametrize("how", ["flag", "env"])
def test_config_explicit_missing_file_exits_2(tmp_path, monkeypatch, capsys, how):
    missing = tmp_path / "nope.ini"
    argv = ["--config", str(missing)] if how == "flag" else []
    if how == "env":
        monkeypatch.setenv("POWER_WATCH_CONFIG", str(missing))
    rc, r, err = run_once(tmp_path, monkeypatch, capsys, argv)
    assert rc == 2 and r is None and str(missing) in err and "not found" in err


@pytest.mark.parametrize("text, key", [
    ("[adapter]\nvoltage_v = nineteen\n", "[adapter] voltage_v"),
    ("[adapter]\nrating_w = -5\n", "[adapter] rating_w"),
    ("[alert]\nthreshold_w = 0\n", "[alert] threshold_w"),
    ("[alert]\nsustain_s = -1\n", "[alert] sustain_s"),
    ("[estimate]\nrest_w = 500\n", "[estimate] rest_w"),
    ("[estimate]\npsys = maybe\n", "[estimate] psys"),
    ("[gpu]\nenabled = perhaps\n", "[gpu] enabled"),
    ("[gpu]\ndevices = 01:00.0, gpu-two\n", "[gpu] devices"),
])
def test_config_bad_value_exits_2_naming_the_key(tmp_path, monkeypatch, capsys, text, key):
    path = write_config(tmp_path / "bad.ini", text)
    rc, r, err = run_once(tmp_path, monkeypatch, capsys, ["--config", str(path)])
    assert rc == 2 and r is None
    assert f"{path} {key}" in err
    value = text.split("=", 1)[1].strip()
    if any(c.isalpha() for c in value):
        assert value not in err  # the value is not echoed


@pytest.mark.parametrize("text, line", [
    ("[adapter]\nsecret-token-xyz\n", "line 2"),
    ("secret-token-xyz = 1\n[adapter]\n", "line 1"),
    ("[adapter]\nvoltage_v = 1\nvoltage_v = secret-token-xyz\n", "line 3"),
])
def test_config_parse_error_names_line_not_content(tmp_path, monkeypatch, capsys, text, line):
    path = write_config(tmp_path / "broken.ini", text)
    rc, _, err = run_once(tmp_path, monkeypatch, capsys, ["--config", str(path)])
    assert rc == 2 and str(path) in err and line in err
    assert "secret-token-xyz" not in err


def test_config_unknown_key_warns(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path / "c.ini", "[adapter]\nvolts = 19.5\n[gpu]\ncolour = red\n")
    rc, r, err = run_once(tmp_path, monkeypatch, capsys, ["--config", str(path)])
    assert rc == 0 and r["adapter_v"] is None
    assert f"{path} [adapter] volts: unknown key" in err and "[gpu] colour" in err


def test_config_empty_value_means_unset(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path / "c.ini", "[adapter]\nvoltage_v =\n[gpu]\ndevices =\n")
    rc, r, err = run_once(tmp_path, monkeypatch, capsys, ["--config", str(path)])
    assert rc == 0 and err == "" and r["adapter_v"] is None and r["gpu_status"] == "active"


def test_default_threshold_from_rating():
    assert pw.Config().threshold == 150.0
    assert pw.Config(rating_w=200.0).threshold == pytest.approx(160.0)
    assert pw.Config(rating_w=200.0, threshold_w=90.0).threshold == 90.0


def test_threshold_from_rating_in_report(tmp_path, monkeypatch, capsys):
    rc, r, _ = run_once(tmp_path, monkeypatch, capsys, ["--adapter-rating", "90"])
    assert rc == 0 and r["threshold_w"] == pytest.approx(72.0) and r["adapter_rating_w"] == 90.0


def test_bad_flag_values_exit_2(tmp_path, monkeypatch, capsys):
    for argv in (["--gpu", "not-a-pci"], ["--adapter-voltage", "0"], ["--sustain", "-1"],
                 ["--rest", "999"], ["--no-gpu", "--gpu", "01:00.0"]):
        with pytest.raises(SystemExit) as exc:
            pw.parse_args(argv)
        assert exc.value.code == 2


def test_normalise_pci():
    assert pw.normalise_pci("01:00.0") == "0000:01:00.0"
    assert pw.normalise_pci("00000000:01:00.0") == "0000:01:00.0"
    assert pw.normalise_pci("0000:0A:1f.3") == "0000:0a:1f.3"
    assert pw.normalise_pci("1:0.0") == "0000:01:00.0"
    for bad in ("", "01:00", "gpu", "0000:01:00.8"):
        assert pw.normalise_pci(bad) is None


def test_unknown_voltage_and_rating_render():
    r = pw.model(sample(), pw.Calibration(), pw.Config())
    assert (r["adapter_v"], r["adapter_current_a"], r["adapter_rating_w"]) == (None, None, None)
    out = pw.render(r)
    assert " A at " not in out and "% of" not in out
    assert "rating not configured" in out and "rating_w under [adapter]" in out
    assert "--adapter-rating" in out


def test_rating_hint_names_the_config_in_effect(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys")
    default = tmp_path / "xdg-config" / "power-watch" / "config"
    assert f"rating_w under [adapter] in {default}," in pw.render(
        pw.model(sample(), pw.Calibration(), pw.Config()))
    path = write_config(tmp_path / "mine.ini", "[alert]\nthreshold_w = 120\n")
    for argv in (["--config", str(path)], []):
        if not argv:
            monkeypatch.setenv("POWER_WATCH_CONFIG", str(path))
        rc = run_main(["--once", "--window", "0.05", *argv], FakeClock(), monkeypatch, root,
                      tmp_path)
        out = capsys.readouterr().out
        assert rc == 0
        assert f"rating_w under [adapter] in {path}," in out and str(default) not in out
    cfg = pw.load_config(str(path))
    monitor = pw.Monitor(None, tmp_path / "state", cfg)
    r = pw.model(sample(), pw.Calibration(), cfg)
    assert f"in {path}," in pw.render(r, monitor, 0.0)


def test_default_section_is_an_ignored_unknown_section(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path / "c.ini", "[DEFAULT]\nrating_w = 99\nbogus = 1\n"
                                            "[adapter]\nvoltage_v = 20\n")
    rc, r, err = run_once(tmp_path, monkeypatch, capsys, ["--config", str(path)])
    assert rc == 0 and err == ""        # no key copied into [adapter], no false warning
    assert r["adapter_v"] == 20.0 and r["adapter_rating_w"] is None


def test_config_example_loads_cleanly_with_its_inline_comment_form(tmp_path, monkeypatch, capsys):
    example = (SCRIPT.parent.parent / "config.example").read_text()
    path = write_config(tmp_path / "example.ini", example)
    rc, r, err = run_once(tmp_path, monkeypatch, capsys, ["--config", str(path)])
    assert rc == 0 and err == "" and r["adapter_rating_w"] is None
    inline = write_config(tmp_path / "inline.ini", "[adapter]\nrating_w = 330  # from the label\n"
                                                   "voltage_v = 19.5 ; volts\n")
    rc, r, err = run_once(tmp_path, monkeypatch, capsys, ["--config", str(inline)])
    assert rc == 0 and err == "" and (r["adapter_rating_w"], r["adapter_v"]) == (330.0, 19.5)


def test_relative_xdg_config_home_is_ignored(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path / "rel" / "power-watch" / "config", "[alert]\nthreshold_w = 101\n")
    write_config(tmp_path / "home" / ".config" / "power-watch" / "config",
                 "[alert]\nthreshold_w = 102\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", "rel")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert run_once(tmp_path, monkeypatch, capsys)[1]["threshold_w"] == 102.0


def test_known_rating_unknown_voltage_render():
    out = pw.render(pw.model(sample(), pw.Calibration(), pw.Config(rating_w=100.0)))
    assert " A at " not in out and "72 % of 100 W" in out


# ------------------------------------------------------------ GPU discovery

@pytest.mark.parametrize("runtime", ["suspended", "suspending"])
def test_suspended_nvidia_is_not_queried(tmp_path, monkeypatch, capsys, runtime):
    root = make_sysfs(tmp_path / "sys", gpus=())
    add_gpu(root, runtime=runtime)
    rc, r, err = run_once(tmp_path, monkeypatch, capsys, root=root)
    assert rc == 0, err
    assert smi_calls(tmp_path) == []  # nvidia-smi would wake the GPU
    assert r["gpu_status"] == "suspended" and r["gpu_w"] == 0.0 and r["incomplete"] is False
    assert r["gpu_limit_w"] is None and r["gpu_pstate"] is None and r["errors"] == []
    assert r["gpu_devices"] == [{"pci": GPU_PCI, "vendor": "nvidia", "status": "suspended",
                                 "power_w": 0.0}]
    assert "suspended, not queried" in pw.render(r)


def test_no_discrete_gpu_never_runs_nvidia_smi(tmp_path):
    root = make_sysfs(tmp_path / "sys", gpus=())
    gpu = pw.read_gpus(root, fake_smi(tmp_path))
    assert (gpu.state, gpu.power_w, gpu.devices) == ("none", 0.0, [])
    assert smi_calls(tmp_path) == []


def test_amd_dgpu_power1_average_and_input_fallback(tmp_path):
    root = make_sysfs(tmp_path / "sys", gpus=())
    add_gpu(root, "0000:03:00.0", "amd",
            hwmon={"power1_average": 55_500_000, "power1_input": 1, "power1_cap": 180_000_000,
                   "power1_cap_max": 200_000_000})
    add_gpu(root, "0000:04:00.0", "amd", hwmon={"power1_input": 20_000_000})
    gpu = pw.read_gpus(root, fake_smi(tmp_path))
    assert gpu.state == "active" and gpu.power_w == pytest.approx(75.5)
    assert gpu.limit_w == pytest.approx(180.0) and gpu.max_limit_w == pytest.approx(200.0)
    assert gpu.pstate is None and smi_calls(tmp_path) == []
    assert [(d.pci, d.vendor, d.power_w) for d in gpu.devices] == [
        ("0000:03:00.0", "amd", 55.5), ("0000:04:00.0", "amd", 20.0)]


def test_amd_dgpu_without_hwmon_is_unknown(tmp_path):
    root = make_sysfs(tmp_path / "sys", gpus=())
    add_gpu(root, "0000:03:00.0", "amd")
    gpu = pw.read_gpus(root, fake_smi(tmp_path))
    assert gpu.state == "unknown" and gpu.power_w is None and "0000:03:00.0" in gpu.error


def test_amd_apu_and_intel_igpu_are_skipped(tmp_path):
    root = make_sysfs(tmp_path / "sys", gpus=())
    add_gpu(root, "0000:00:02.0", "intel")
    add_gpu(root, "0000:05:00.0", "amd", hwmon={"in1_input": 700, "power1_input": 30_000_000})
    gpu = pw.read_gpus(root, fake_smi(tmp_path))
    assert (gpu.state, gpu.power_w, gpu.devices) == ("none", 0.0, [])


def test_suspended_amd_hwmon_not_read(tmp_path):
    root = make_sysfs(tmp_path / "sys", gpus=())
    dev = add_gpu(root, "0000:03:00.0", "amd", runtime="suspended",
                  hwmon={"power1_average": 50_000_000})
    (dev / "hwmon" / "hwmon3" / "power1_average").chmod(0o000)  # a read would fail loudly
    gpu = pw.read_gpus(root, fake_smi(tmp_path))
    assert gpu.state == "suspended" and gpu.power_w == 0.0


def test_two_dgpus_summed(tmp_path):
    root = make_sysfs(tmp_path / "sys", gpus=(GPU_PCI, "0000:02:00.0"))
    smi = fake_smi(tmp_path, output=["00000000:01:00.0, 30.00, 150.00, 1.00, 160.00, P2",
                                     "00000000:02:00.0, 12.50, 100.00, 1.00, 120.00, P8"])
    gpu = pw.read_gpus(root, smi)
    assert gpu.power_w == pytest.approx(42.5) and gpu.limit_w == pytest.approx(250.0)
    assert gpu.max_limit_w == pytest.approx(280.0) and gpu.pstate == "P2"
    assert smi_calls(tmp_path) == [f"--query-gpu={pw.GPU_QUERY} --format=csv,noheader,nounits"]


def test_nvidia_plus_amd_summed(tmp_path):
    root = make_sysfs(tmp_path / "sys")
    add_gpu(root, "0000:03:00.0", "amd", hwmon={"power1_average": 10_000_000})
    gpu = pw.read_gpus(root, fake_smi(tmp_path))
    assert gpu.power_w == pytest.approx(46.7) and gpu.state == "active"


def test_one_unknown_gpu_keeps_known_reading(tmp_path):
    root = make_sysfs(tmp_path / "sys")
    add_gpu(root, "0000:03:00.0", "amd")  # no hwmon: unknown
    gpu = pw.read_gpus(root, fake_smi(tmp_path))
    assert gpu.state == "unknown" and gpu.power_w == pytest.approx(36.7)
    r = pw.model(sample(gpu=gpu), pw.Calibration(), CFG)
    assert r["incomplete"] is True and r["total_lower_bound"] is True and r["gpu_w"] == 36.7


def test_no_gpu_flag_queries_nothing(tmp_path, monkeypatch, capsys):
    rc, r, _ = run_once(tmp_path, monkeypatch, capsys, ["--no-gpu"])
    assert rc == 0 and smi_calls(tmp_path) == []
    assert (r["gpu_status"], r["gpu_w"], r["gpu_devices"], r["incomplete"]) == ("disabled", 0.0, [], False)
    path = write_config(tmp_path / "c.ini", "[gpu]\nenabled = no\n")
    rc, r, _ = run_once(tmp_path, monkeypatch, capsys, ["--config", str(path)])
    assert r["gpu_status"] == "disabled" and smi_calls(tmp_path) == []


def test_gpu_selection_passes_subset_ids(tmp_path, monkeypatch, capsys):
    root = make_sysfs(tmp_path / "sys", gpus=(GPU_PCI, "0000:02:00.0"))
    smi = fake_smi(tmp_path, output="00000000:02:00.0, 12.50, 100.00, 1.00, 120.00, P8")
    rc, r, _ = run_once(tmp_path, monkeypatch, capsys, ["--gpu", "02:00.0"], root=root, smi=smi)
    assert rc == 0 and r["gpu_w"] == 12.5 and r["gpu_status"] == "active"
    assert [d["pci"] for d in r["gpu_devices"]] == ["0000:02:00.0"]
    assert smi_calls(tmp_path) == [
        f"-i 0000:02:00.0 --query-gpu={pw.GPU_QUERY} --format=csv,noheader,nounits"]
    path = write_config(tmp_path / "c.ini", "[gpu]\ndevices = 0000:02:00.0\n")
    rc, r, _ = run_once(tmp_path, monkeypatch, capsys, ["--config", str(path)], root=root, smi=smi)
    assert r["gpu_w"] == 12.5 and smi_calls(tmp_path)[-1].startswith("-i 0000:02:00.0 ")


def test_suspended_nvidia_left_out_of_the_query(tmp_path):
    root = make_sysfs(tmp_path / "sys", gpus=())
    add_gpu(root, GPU_PCI, runtime="suspended")
    add_gpu(root, "0000:02:00.0")
    smi = fake_smi(tmp_path, output="00000000:02:00.0, 12.50, 100.00, 1.00, 120.00, P8")
    gpu = pw.read_gpus(root, smi)
    assert gpu.state == "active" and gpu.power_w == pytest.approx(12.5)
    assert smi_calls(tmp_path)[0].startswith("-i 0000:02:00.0 ")


def test_selected_gpu_missing_is_reported(tmp_path):
    root = make_sysfs(tmp_path / "sys")
    gpu = pw.read_gpus(root, fake_smi(tmp_path), select=("0000:09:00.0",))
    assert gpu.state == "none" and "0000:09:00.0" in gpu.error and smi_calls(tmp_path) == []


# ---------------------------------------------------------------------- psys

def psys_sampler(tmp_path, root, psys_w=60.0, cpu_w=10.0, config=None):
    clock = FakeClock()
    clock.hooks.append(lambda t, dt: add_energy(root, cpu_w * 1e6 * dt))
    if psys_w is not None:
        clock.hooks.append(lambda t, dt: add_energy(root, psys_w * 1e6 * dt, "intel-rapl:1"))
    return pw.Sampler(root, fake_smi(tmp_path), clock=clock, sleep=clock.sleep, config=config)


def test_psys_is_the_measured_total_on_ac(tmp_path):
    root = make_sysfs(tmp_path / "sys", status="Charging", current_ua=1_000_000)
    add_psys(root)
    s = psys_sampler(tmp_path, root).once(2.0)
    assert s.cpu_w == pytest.approx(10.0) and s.psys_w == pytest.approx(60.0)  # never summed
    r = pw.model(s, pw.Calibration(), CFG)
    assert (r["total_w"], r["total_kind"], r["total_source"]) == (60.0, "measured", "psys")
    assert r["incomplete"] is False and r["total_lower_bound"] is False and r["psys_w"] == 60.0
    assert r["charge_w"] == 16.0  # reported, not added
    assert r["adapter_current_a"] == pytest.approx(round(60.0 / 19.5, 2))
    assert pw.rest_sample(r) is None
    out = pw.render(r)
    assert "measured platform power (RAPL psys)" in out and "not added" in out


def test_psys_off_falls_back_to_estimate(tmp_path):
    root = make_sysfs(tmp_path / "sys")
    add_psys(root)
    cfg = dataclasses.replace(CFG, psys=False)
    s = psys_sampler(tmp_path, root, config=cfg).once(2.0)
    r = pw.model(s, pw.Calibration(), cfg)
    assert s.psys_w is None and r["psys_w"] is None
    assert r["total_source"] == "model" and r["total_kind"] == "estimated"
    assert r["total_w"] == pytest.approx(10.0 + 36.7 + 25.0)
    assert pw.parse_args(["--no-psys"]).no_psys is True


def test_psys_unreadable_or_zero_is_not_an_error(tmp_path):
    root = make_sysfs(tmp_path / "sys")
    zone = add_psys(root)
    s = psys_sampler(tmp_path, root, psys_w=0.0).once(2.0)
    r = pw.model(s, pw.Calibration(), CFG)
    assert r["psys_w"] is None and r["total_source"] == "model" and r["errors"] == []
    (zone / "energy_uj").unlink()
    s = psys_sampler(tmp_path, root, psys_w=None).once(2.0)
    assert s.psys_w is None and s.errors == []


def test_psys_ignored_on_battery(tmp_path):
    root = make_sysfs(tmp_path / "sys", ac=0, status="Discharging", current_ua=4_000_000)
    add_psys(root)
    r = pw.model(psys_sampler(tmp_path, root).once(2.0), pw.Calibration(), CFG)
    assert r["total_source"] == "battery" and r["total_w"] == 64.0 and r["total_kind"] == "measured"
    assert pw.rest_sample(r) == pytest.approx(64.0 - 10.0 - 36.7)


def test_alerter_fires_on_psys(tmp_path):
    notify, log = fake_notify(tmp_path)
    a = pw.Alerter(threshold=150, sustain=0, every=0, notify=notify)
    assert a.update(report(200.0, kind="measured", total_source="psys", cpu_w=None), 0) is True
    body = log.read_text().split("|")[3]
    assert body.startswith("Measured adapter load 200 W") and "lower bound" not in body


# --------------------------------------------------------------- RAPL limit

def test_read_cpu_limit_sums_discovered_package_zones(tmp_path):
    root = make_sysfs(tmp_path / "sys", packages=(1, 2))
    zone = add_psys(root, 0)  # intel-rapl:0 is psys here, not a package
    write(zone / "constraint_0_power_limit_uw", 999_000_000)
    assert pw.read_cpu_limit(root) is None  # package limits unreadable
    write(root / "class/powercap/intel-rapl:1/constraint_0_power_limit_uw", 45_000_000)
    assert pw.read_cpu_limit(root) is None  # one of two
    write(root / "class/powercap/intel-rapl:2/constraint_0_power_limit_uw", 30_000_000)
    assert pw.read_cpu_limit(root) == pytest.approx(75.0)
    assert pw.read_cpu_limit(tmp_path / "none") is None


# ----------------------------------------------------------- multi-battery

def add_battery(root, name, status, *, current_ua=0, voltage_uv=16_000_000, capacity=None,
                energy=None, charge=None):
    bat = root / "class" / "power_supply" / name
    write(bat / "status", status)
    write(bat / "current_now", current_ua)
    write(bat / "voltage_now", voltage_uv)
    if capacity is not None:
        write(bat / "capacity", capacity)
    for prefix, pair in (("energy", energy), ("charge", charge)):
        if pair:
            write(bat / f"{prefix}_now", pair[0])
            write(bat / f"{prefix}_full", pair[1])
    return bat


def test_multi_battery_sum_status_and_energy_capacity(tmp_path):
    root = tmp_path / "sys"
    add_battery(root, "BAT0", "Not charging", current_ua=1_000_000, capacity=90,
                energy=(45_000_000, 50_000_000))
    add_battery(root, "BAT1", "Discharging", current_ua=2_000_000, capacity=10,
                energy=(2_000_000, 20_000_000))
    status, capacity, watts = pw.read_batteries(root)
    assert status == "Discharging" and watts == pytest.approx(48.0)
    assert capacity == round(100 * 47 / 70)  # energy-weighted, not the mean (50)


def test_multi_battery_charging_and_mean_capacity(tmp_path):
    root = tmp_path / "sys"
    add_battery(root, "BAT0", "Full", capacity=100, energy=(50, 50))
    add_battery(root, "BAT1", "Charging", current_ua=500_000, capacity=41)  # no energy_*
    status, capacity, watts = pw.read_batteries(root)
    assert status == "Charging" and capacity == round((100 + 41) / 2)
    assert watts == pytest.approx(8.0)


def test_multi_battery_charge_capacity_and_first_status(tmp_path):
    root = tmp_path / "sys"
    add_battery(root, "BAT0", "Not charging", charge=(1_000_000, 4_000_000))
    add_battery(root, "BAT1", "Full", charge=(3_000_000, 4_000_000))
    assert pw.read_batteries(root)[:2] == ("Not charging", 50)


def test_multi_battery_unreadable_power_is_unknown(tmp_path):
    root = tmp_path / "sys"
    add_battery(root, "BAT0", "Discharging", current_ua=1_000_000)
    (add_battery(root, "BAT1", "Discharging") / "voltage_now").unlink()
    assert pw.read_batteries(root)[2] is None


# ------------------------------------------------------ lower-bound alerts

def test_alert_fires_with_gpu_unknown_labelled_lower_bound(tmp_path):
    notify, log = fake_notify(tmp_path)
    s = sample(cpu_w=90.0, gpu=pw.Gpu(error="GPU power unknown: nvidia-smi not found"))
    r = pw.model(s, pw.Calibration(), CFG)
    assert r["total_lower_bound"] is True and r["total_w"] == pytest.approx(115.0)
    a = pw.Alerter(threshold=100, sustain=0, every=0, notify=notify)
    assert a.update(r, 0) is True
    args = log.read_text().splitlines()[0].split("|")
    assert args[2] == "Power adapter overload: ≥ ~115 W"
    assert "lower bound: GPU power unknown" in args[3] and "Estimated adapter load 115 W" in args[3]


def test_alert_without_voltage_has_no_amps(tmp_path):
    notify, log = fake_notify(tmp_path)
    r = pw.model(sample(cpu_w=200.0), pw.Calibration(), pw.Config())
    assert pw.Alerter(threshold=150, sustain=0, every=0, notify=notify).update(r, 0) is True
    body = log.read_text().split("|")[3]
    assert " A at " not in body and "above 150 W" in body


def test_rest_sample_refuses_unknown_gpu():
    gpu = pw.combine_gpus([pw.GpuDevice("0000:01:00.0", "nvidia", power_w=30.0),
                           pw.GpuDevice("0000:02:00.0", "amd", status="unknown")])
    s = sample(ac_online=False, battery_status="Discharging", battery_w=64.0, gpu=gpu)
    r = pw.model(s, pw.Calibration(), CFG)
    assert r["gpu_w"] == 30.0 and r["gpu_status"] == "unknown" and pw.rest_sample(r) is None


# ------------------------------------------------------------ report schema

def test_report_keys_and_frozen_csv_header():
    assert pw.LOG_KEYS == [
        "time", "ac_online", "battery_status", "battery_capacity", "battery_w",
        "cpu_w", "gpu_w", "gpu_limit_w", "gpu_pstate", "rest_w", "rest_source",
        "charge_w", "total_w", "total_kind", "incomplete", "adapter_v",
        "adapter_current_a", "adapter_rating_w", "threshold_w"]
    assert pw.REPORT_KEYS == pw.LOG_KEYS + [
        "errors", "total_source", "total_lower_bound", "psys_w", "gpu_status", "gpu_devices"]
    r = pw.model(sample(), pw.Calibration(), CFG)
    assert list(r) == pw.REPORT_KEYS


def test_once_json_with_owner_config_matches_old_values(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path / "owner.ini",
                        "[adapter]\nvoltage_v = 19.5\nrating_w = 330\n"
                        "[alert]\nthreshold_w = 150\nsustain_s = 10\n"
                        "[power-cap]\nsafe_cpu_w = 45\nsafe_gpu_w = 80\n")
    rc, r, err = run_once(tmp_path, monkeypatch, capsys, ["--config", str(path)])
    assert rc == 0 and err == ""
    assert (r["adapter_v"], r["adapter_rating_w"], r["threshold_w"]) == (19.5, 330.0, 150.0)
    assert (r["gpu_w"], r["gpu_limit_w"], r["gpu_pstate"]) == (36.7, 190.0, "P0")
    assert (r["total_source"], r["total_lower_bound"], r["psys_w"], r["gpu_status"]) == (
        "model", False, None, "active")
    assert r["adapter_current_a"] == pytest.approx(round(r["total_w"] / 19.5, 2))


# ------------------------------------------------- cpu_cap (power-cap's applied.json)

def cap_dir(tmp_path, applied=None, originals=False):
    """A power-cap state dir: applied.json (dict -> JSON, str as is) and/or originals.json."""
    d = tmp_path / "run-power-cap"
    d.mkdir(exist_ok=True)
    if applied is not None:
        (d / "applied.json").write_text(applied if isinstance(applied, str) else json.dumps(applied))
    if originals:
        (d / "originals.json").write_text("{}")
    return d


def record(**zones):
    """applied.json content; zone keyword intel_rapl_N names zone intel-rapl:N."""
    return {"zones": {"intel-rapl:" + name.rsplit("_", 1)[1]: entry
                      for name, entry in zones.items()}, "time": 1.0}


PL = "constraint_0_power_limit_uw"
PL2 = "constraint_1_power_limit_uw"


def test_cpu_cap_capped_and_tolerance(sysfs, tmp_path):
    write(sysfs / "class/powercap/intel-rapl:0" / PL, 45_000_000)
    write(sysfs / "class/powercap/intel-rapl:0" / PL2, 45_000_000)
    d = cap_dir(tmp_path, record(intel_rapl_0={PL: 45_000_000, PL2: 45_000_000}))
    assert pw.read_cpu_cap(sysfs, d) == ("capped", 45.0)
    write(sysfs / "class/powercap/intel-rapl:0" / PL, 45_999_000)        # RAPL unit rounding
    assert pw.read_cpu_cap(sysfs, d) == ("capped", 45.0)


def test_cpu_cap_w_sums_pl1_over_zones(tmp_path):
    root = make_sysfs(tmp_path / "sys", packages=(0, 1))
    for n, uw in ((0, 30_000_000), (1, 20_500_000)):
        write(root / f"class/powercap/intel-rapl:{n}" / PL, uw)
    d = cap_dir(tmp_path, record(intel_rapl_0={PL: 30_000_000}, intel_rapl_1={PL: 20_500_000}))
    assert pw.read_cpu_cap(root, d) == ("capped", 50.5)


@pytest.mark.parametrize("change", ["pl1", "pl2", "disabled", "zone-missing"])
def test_cpu_cap_reverted(sysfs, tmp_path, change):
    zone = sysfs / "class/powercap/intel-rapl:0"
    write(zone / PL, 45_000_000)
    write(zone / PL2, 45_000_000)
    rec = record(intel_rapl_0={PL: 45_000_000, PL2: 45_000_000})
    if change == "pl1":
        write(zone / PL, 91_000_000)           # PL1 back to the firmware value (nostromo)
    elif change == "pl2":
        write(zone / PL2, 46_500_000)
    elif change == "disabled":
        write(zone / "enabled", 0)
    else:
        rec["zones"]["intel-rapl:7"] = {PL: 45_000_000}
    assert pw.read_cpu_cap(sysfs, cap_dir(tmp_path, rec)) == ("reverted", 90.0
                                                              if change == "zone-missing" else 45.0)


def test_cpu_cap_uncapped_only_when_both_files_are_absent(sysfs, tmp_path):
    assert pw.read_cpu_cap(sysfs, tmp_path / "missing") == ("uncapped", None)
    assert pw.read_cpu_cap(sysfs, cap_dir(tmp_path)) == ("uncapped", None)


def test_cpu_cap_null_for_a_cap_by_an_older_power_cap(sysfs, tmp_path):
    """originals.json without applied.json: only an older power-cap leaves that (every `set`
    of the current one writes a record)."""
    assert pw.read_cpu_cap(sysfs, cap_dir(tmp_path, originals=True)) == (None, None)


@pytest.mark.parametrize("originals", [False, True])
def test_cpu_cap_uncapped_for_a_record_of_no_zones(sysfs, tmp_path, originals):
    """"zones": {}: power-cap holds no CPU limit (a GPU-only set, refused CPU writes, or a
    reset that restored the CPU but not everything else)."""
    d = cap_dir(tmp_path, {"zones": {}, "time": 1.0}, originals=originals)
    assert pw.read_cpu_cap(sysfs, d) == ("uncapped", None)


@pytest.mark.parametrize("content", [
    "{not json", "[]", "null", '{}', '{"zones": []}',
    '{"zones": {"../../etc": {"constraint_0_power_limit_uw": 1}}}',
    '{"zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": 4.5e7}}}',
    '{"zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": false}}}',
    '{"zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": -1}}}',
    '{"zones": {"intel-rapl:0": {"enabled": 1}}}', '{"zones": {"intel-rapl:0": {}}}',
    "{}" + " " * (64 * 1024), b"\xff\xfe",
])
def test_cpu_cap_null_for_a_malformed_or_oversized_record(sysfs, tmp_path, content):
    d = cap_dir(tmp_path)
    if isinstance(content, bytes):
        (d / "applied.json").write_bytes(content)
    else:
        (d / "applied.json").write_text(content)
    assert pw.read_cpu_cap(sysfs, d) == (None, None)


def test_cpu_cap_record_of_exactly_64_kib_is_read(sysfs, tmp_path):
    write(sysfs / "class/powercap/intel-rapl:0" / PL, 45_000_000)
    text = json.dumps(record(intel_rapl_0={PL: 45_000_000}))
    d = cap_dir(tmp_path, text + " " * (64 * 1024 - len(text)))
    assert (d / "applied.json").stat().st_size == 64 * 1024
    assert pw.read_cpu_cap(sysfs, d) == ("capped", 45.0)


def test_cpu_cap_null_for_a_non_regular_file(sysfs, tmp_path):
    d = cap_dir(tmp_path)
    os.mkfifo(d / "applied.json")              # a blocking open would hang the monitor
    assert pw.read_cpu_cap(sysfs, d) == (None, None)
    (d / "applied.json").unlink()
    (d / "applied.json").mkdir()
    assert pw.read_cpu_cap(sysfs, d) == (None, None)


def test_cpu_cap_null_on_permission_errors(sysfs, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    write(sysfs / "class/powercap/intel-rapl:0" / PL, 45_000_000)
    d = cap_dir(tmp_path, record(intel_rapl_0={PL: 45_000_000}))
    (d / "applied.json").chmod(0o000)
    assert pw.read_cpu_cap(sysfs, d) == (None, None)
    (d / "applied.json").unlink()
    d.chmod(0o000)                             # a root-only 0700 dir from an older power-cap
    try:
        assert pw.read_cpu_cap(sysfs, d) == (None, None)
    finally:
        d.chmod(0o755)


def test_cpu_cap_null_when_a_live_limit_is_unreadable(sysfs, tmp_path):
    d = cap_dir(tmp_path, record(intel_rapl_0={PL: 45_000_000, PL2: 45_000_000}))
    write(sysfs / "class/powercap/intel-rapl:0" / PL, 45_000_000)
    assert pw.read_cpu_cap(sysfs, d) == (None, 45.0)          # constraint_1 file missing
    write(sysfs / "class/powercap/intel-rapl:0" / PL2, 45_000_000)
    (sysfs / "class/powercap/intel-rapl:0/enabled").unlink()
    assert pw.read_cpu_cap(sysfs, d) == (None, 45.0)


def test_cpu_cap_w_null_when_only_pl2_was_recorded(sysfs, tmp_path):
    write(sysfs / "class/powercap/intel-rapl:0" / PL2, 45_000_000)
    d = cap_dir(tmp_path, record(intel_rapl_0={PL2: 45_000_000}))
    assert pw.read_cpu_cap(sysfs, d) == ("capped", None)


def test_cpu_cap_never_raises(sysfs, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("surprise")

    d = cap_dir(tmp_path, record(intel_rapl_0={PL: 45_000_000}))
    monkeypatch.setattr(pw, "read_int", boom)
    assert pw.read_cpu_cap(sysfs, d) == (None, None)


def test_cpu_cap_reads_power_cap_state_dir_each_sample(sysfs, tmp_path, monkeypatch):
    write(sysfs / "class/powercap/intel-rapl:0" / PL, 45_000_000)
    d = cap_dir(tmp_path)
    monkeypatch.setenv("POWER_CAP_STATE_DIR", str(d))
    assert pw.read_cpu_cap(sysfs) == ("uncapped", None)
    (d / "applied.json").write_text(json.dumps(record(intel_rapl_0={PL: 45_000_000})))
    assert pw.read_cpu_cap(sysfs) == ("capped", 45.0)
    monkeypatch.delenv("POWER_CAP_STATE_DIR")
    assert pw.cap_state_dir() == Path("/run/power-cap")
