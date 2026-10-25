"""Tests for bin/power-cap on a fixture sysfs tree and a fake nvidia-smi."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "power-cap"


def load_module():
    loader = importlib.machinery.SourceFileLoader("power_cap", str(SCRIPT))
    spec = importlib.util.spec_from_loader("power_cap", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


pc = load_module()

# Fake nvidia-smi: state in files next to it. mode = accept | refuse | error.
FAKE_SMI = r"""#!/bin/sh
d="$(dirname "$0")/smi-state"
echo "$*" >> "$d/calls"
case "$*" in
  *--query-gpu=*)
    [ -f "$d/query_fail" ] && exit 9
    printf '36.70, %s, 1.00, 190.00, P0\n' "$(cat "$d/limit")"
    exit 0 ;;
esac
pl=""; prev=""
for a in "$@"; do [ "$prev" = "-pl" ] && pl=$a; prev=$a; done
case "$(cat "$d/mode")" in
  accept) printf '%.2f\n' "$pl" > "$d/limit"; echo "Power limit for GPU 0 was set to $pl W" ;;
  refuse) echo "Power limit for GPU 0 was set to $pl W" ;;
  error) echo "Changing power management limit is not supported for GPU: 00000000:01:00.0." >&2
         echo "Treating as warning and moving on." >&2; exit 3 ;;
esac
"""

# Fake systemctl: argv lines in systemctl-state/calls (one argument per field, tab-separated),
# thermald state in systemctl-state/thermald (active | inactive). stop_fail / start_fail make
# that verb fail; stop_noop leaves thermald active. stop records PL1 at that moment.
FAKE_SYSTEMCTL = r"""#!/bin/sh
d="$(dirname "$0")/systemctl-state"
(IFS="$(printf '\t')"; echo "$*") >> "$d/calls"
verb=""
for a in "$@"; do case "$a" in --*) ;; *) verb=$a; break ;; esac; done
case "$verb" in
  is-active)
    state="$(cat "$d/thermald")"; echo "$state"
    [ "$state" = active ] && exit 0; exit 3 ;;
  stop)
    cat "$POWER_WATCH_SYSFS/class/powercap/intel-rapl:0/constraint_0_power_limit_uw" \
      >> "$d/pl1_at_stop"
    if [ -f "$d/stop_fail" ]; then
      echo "Failed to stop thermald.service: Access denied" >&2; exit 1
    fi
    [ -f "$d/stop_noop" ] || echo inactive > "$d/thermald"; exit 0 ;;
  start)
    if [ -f "$d/start_fail" ]; then
      echo "Job for thermald.service failed." >&2; exit 1
    fi
    echo active > "$d/thermald"; exit 0 ;;
esac
exit 1
"""


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "sys"
        self.zone = self.root / "class" / "powercap" / "intel-rapl:0"
        self.state = tmp_path / "run" / "power-cap"
        self.smi = tmp_path / "nvidia-smi"
        self.smi_state = tmp_path / "smi-state"
        self.systemctl = tmp_path / "systemctl"
        self.sc_state = tmp_path / "systemctl-state"
        files = {
            "name": "package-0", "enabled": "1", "energy_uj": "123456",
            "max_energy_range_uj": "262143328850",
            "constraint_0_name": "long_term", "constraint_0_power_limit_uw": "91000000",
            "constraint_0_max_power_uw": "91000000", "constraint_0_time_window_us": "7995392",
            "constraint_1_name": "short_term", "constraint_1_power_limit_uw": "0",
            "constraint_1_time_window_us": "2440",
        }
        for name, value in files.items():
            self.write(name, value)
        for sub, name in (("intel-rapl:0:0", "core"), ("intel-rapl:0:1", "uncore")):
            d = self.root / "class" / "powercap" / sub
            d.mkdir(parents=True)
            (d / "name").write_text(name + "\n")
            (d / "constraint_0_power_limit_uw").write_text("0\n")
        # the NVIDIA GPU (display function) and its HDMI audio function, as on a real card
        self.gpu_dev = self.pci("0000:01:00.0", "0x030000", "active")
        self.pci("0000:01:00.1", "0x040300", "suspended")
        self.smi.write_text(FAKE_SMI)
        self.smi.chmod(0o755)
        self.smi_state.mkdir()
        self.set_gpu(limit="190.00", mode="accept")
        self.systemctl.write_text(FAKE_SYSTEMCTL)
        self.systemctl.chmod(0o755)
        self.sc_state.mkdir()
        self.thermald("inactive")
        # no config file: XDG_CONFIG_HOME points at an empty dir, never the real ~/.config
        self.config_home = tmp_path / "xdg"
        self.vars = {"POWER_WATCH_SYSFS": str(self.root), "POWER_WATCH_NVIDIA_SMI": str(self.smi),
                     "POWER_CAP_STATE_DIR": str(self.state),
                     "POWER_CAP_SYSTEMCTL": str(self.systemctl),
                     "XDG_CONFIG_HOME": str(self.config_home)}
        for k, v in self.vars.items():
            monkeypatch.setenv(k, v)
        for k in ("POWER_WATCH_CONFIG", "SUDO_USER", "SUDO_UID", "SUDO_GID", "DOAS_USER"):
            monkeypatch.delenv(k, raising=False)

    def pci(self, address, cls, runtime, vendor="0x10de"):
        d = self.root / "bus" / "pci" / "devices" / address
        (d / "power").mkdir(parents=True, exist_ok=True)
        (d / "class").write_text(f"{cls}\n")
        (d / "vendor").write_text(f"{vendor}\n")
        (d / "power" / "runtime_status").write_text(f"{runtime}\n")
        return d

    def gpu_runtime(self, status):
        (self.gpu_dev / "power" / "runtime_status").write_text(f"{status}\n")

    def write(self, name, value):
        self.zone.mkdir(parents=True, exist_ok=True)
        (self.zone / name).write_text(f"{value}\n")

    def read(self, name):
        return (self.zone / name).read_text().strip()

    def set_gpu(self, limit=None, mode=None):
        if limit is not None:
            (self.smi_state / "limit").write_text(limit + "\n")
        if mode is not None:
            (self.smi_state / "mode").write_text(mode + "\n")

    def gpu_limit(self):
        return float((self.smi_state / "limit").read_text())

    def smi_calls(self):
        p = self.smi_state / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def originals(self):
        return json.loads((self.state / "originals.json").read_text())

    def applied(self):
        return json.loads((self.state / "applied.json").read_text())

    def thermald(self, state=None):
        """Set the fake thermald state; return the current one."""
        if state is not None:
            (self.sc_state / "thermald").write_text(state + "\n")
        return (self.sc_state / "thermald").read_text().strip()

    def sc_flag(self, name):
        (self.sc_state / name).write_text("")

    def sc_calls(self):
        p = self.sc_state / "calls"
        return [line.split("\t") for line in p.read_text().splitlines()] if p.exists() else []

    def sc_verbs(self):
        return [c for c in self.sc_calls() if c != ["is-active", "thermald.service"]]

    def config(self, text, path=None):
        """Write a config file (default: the XDG location) and return its path."""
        path = path or self.config_home / "power-watch" / "config"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


class FakeIds:
    """Pretend to be root and record effective uid/gid/groups switches instead of making
    them; `opens` records the effective (uid, gid, groups) at each os.open."""

    def __init__(self, monkeypatch):
        self.euid, self.egid, self.groups = 0, 0, [0]
        self.calls, self.opens = [], []
        monkeypatch.setattr(pc, "geteuid", lambda: self.euid)
        monkeypatch.setattr(pc, "getegid", lambda: self.egid)
        monkeypatch.setattr(pc, "getgroups", lambda: list(self.groups))
        monkeypatch.setattr(pc, "getgrouplist", lambda name, gid: [gid, 1500])
        monkeypatch.setattr(pc, "seteuid", self.record("seteuid", "euid"))
        monkeypatch.setattr(pc, "setegid", self.record("setegid", "egid"))
        monkeypatch.setattr(pc, "setgroups", self.record("setgroups", "groups"))
        real_open = os.open

        def tracking_open(path, flags, *a, **kw):
            self.opens.append((str(path), self.euid, self.egid, list(self.groups), flags))
            return real_open(path, flags, *a, **kw)

        monkeypatch.setattr(pc.os, "open", tracking_open)

    def record(self, name, attr):
        def call(value):
            self.calls.append((name, value))
            setattr(self, attr, value)
        return call


@pytest.fixture
def root_ids(monkeypatch):
    return FakeIds(monkeypatch)


def alice(home, uid=1000, gid=1000):
    return lambda name: SimpleNamespace(pw_name=name, pw_dir=str(home), pw_uid=uid, pw_gid=gid)


def run(capsys, *argv):
    code = pc.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# --- status ----------------------------------------------------------------

def test_status_reports_zone_gpu_and_no_cap(env, capsys):
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "intel-rapl:0 (package-0), enabled: yes" in out
    assert "long_term: limit 91.0 W, max 91.0 W, window 8.00 s" in out
    assert "short_term: limit 0.0 W (reads 0 W" in out
    assert "window 2.44 ms" in out
    assert "draw 36.7 W, enforced limit 190.0 W, range 1.0 W .. 190.0 W, pstate P0" in out
    assert "no saved originals" in out
    assert "intel-rapl:0:0" not in out          # subzones are not package zones


def test_status_without_nvidia_smi(env, capsys, monkeypatch):
    monkeypatch.setenv("POWER_WATCH_NVIDIA_SMI", str(env.smi.parent / "missing-nvidia-smi"))
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "GPU (nvidia-smi)\n  n/a" in out


def test_status_gpu_fields_na(env, capsys):
    env.set_gpu(limit="[N/A]")
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "enforced limit n/a" in out


def test_status_shows_active_cap(env, capsys):
    run(capsys, "set", "--cpu", "50")
    code, out, _ = run(capsys, "status")
    assert "Cap: active" in out and "limit 50.0 W" in out


def test_status_unreadable_state_dir_is_unknown_not_none(env, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    run(capsys, "set", "--cpu", "50")
    env.state.chmod(0o000)          # like root-owned 0700 /run/power-cap seen by the user
    try:
        code, out, _ = run(capsys, "status")
    finally:
        env.state.chmod(0o700)
    assert code == 0
    assert "Cap: unknown — saved originals not readable; run sudo power-cap status" in out
    assert "none set" not in out


def test_status_via_subprocess(env):
    r = subprocess.run([sys.executable, str(SCRIPT), "status"], capture_output=True, text=True,
                       env={**os.environ, **env.vars})
    assert r.returncode == 0, r.stderr
    assert "package-0" in r.stdout


# --- set -------------------------------------------------------------------

def test_set_safe_writes_cpu_and_gpu(env, capsys):
    env.config("[power-cap]\nsafe_cpu_w = 45\nsafe_gpu_w = 80\n")
    code, out, _ = run(capsys, "set", "--safe")
    assert code == 0, out
    assert env.read("constraint_0_power_limit_uw") == "45000000"
    assert env.read("constraint_1_power_limit_uw") == "45000000"
    assert "-i 0 -pl 80" in env.smi_calls()
    assert env.gpu_limit() == 80.0
    assert out.count("applied") == 3


def test_set_saves_originals_with_safe_modes(env, capsys):
    run(capsys, "set", "--safe")
    assert env.originals() == {
        "zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": 91000000,
                                   "constraint_1_power_limit_uw": 0, "enabled": 1}},
        "gpu": {"enforced_limit_w": 190.0}}
    assert stat.S_IMODE(env.state.stat().st_mode) == 0o755
    assert stat.S_IMODE((env.state / "originals.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((env.state / "applied.json").stat().st_mode) == 0o644


def test_set_opens_an_old_0700_state_dir_to_0755(env, capsys):
    env.state.mkdir(parents=True, mode=0o700)       # left by an older power-cap
    assert run(capsys, "set", "--cpu", "50")[0] == 0
    assert stat.S_IMODE(env.state.stat().st_mode) == 0o755
    assert stat.S_IMODE((env.state / "originals.json").stat().st_mode) == 0o600


def test_second_set_keeps_originals_and_adds_new_keys(env, capsys):
    run(capsys, "set", "--cpu", "60")
    assert "gpu" not in env.originals()
    run(capsys, "set", "--cpu", "40", "--gpu", "100")
    saved = env.originals()
    assert saved["zones"]["intel-rapl:0"]["constraint_0_power_limit_uw"] == 91000000
    assert saved["zones"]["intel-rapl:0"]["constraint_1_power_limit_uw"] == 0
    assert saved["gpu"] == {"enforced_limit_w": 190.0}
    run(capsys, "set", "--gpu", "70")
    assert env.originals()["gpu"] == {"enforced_limit_w": 190.0}


def test_set_cpu_pl2_separate(env, capsys):
    code, _, _ = run(capsys, "set", "--cpu", "45", "--cpu-pl2", "60")
    assert code == 0
    assert env.read("constraint_0_power_limit_uw") == "45000000"
    assert env.read("constraint_1_power_limit_uw") == "60000000"
    assert env.smi_calls() == []                # no --gpu: GPU untouched


@pytest.mark.parametrize("argv, msg", [
    ([], "at least one of"),
    (["--cpu", "4"], "out of range"),
    (["--cpu", "92"], "out of range"),          # above constraint_0_max_power_uw
    (["--cpu", "45", "--cpu-pl2", "201"], "out of range"),   # no constraint_1 max: 200 W
    (["--cpu-pl2", "50"], "needs --cpu"),
    (["--safe", "--gpu", "100"], "cannot be combined"),
    (["--gpu", "0.5"], "GPU limit 0.5 W out of range (1..190 W)"),
    (["--gpu", "200"], "out of range"),
])
def test_set_validation_errors_write_nothing(env, capsys, argv, msg):
    code, _, err = run(capsys, "set", *argv)
    assert code == 2
    assert msg in err
    assert env.read("constraint_0_power_limit_uw") == "91000000"
    assert not (env.state / "originals.json").exists()
    assert not any("-pl" in c for c in env.smi_calls())


def test_cpu_max_defaults_to_200_without_constraint_max(env, capsys):
    (env.zone / "constraint_0_max_power_uw").unlink()
    assert run(capsys, "set", "--cpu", "150")[0] == 0
    assert run(capsys, "set", "--cpu", "201")[0] == 2


def test_rapl_rounding_within_1w_is_applied(env, capsys, monkeypatch):
    real = pc.write_sysfs
    monkeypatch.setattr(pc, "write_sysfs", lambda p, v: real(p, v - 500000 if v > 1 else v))
    code, out, _ = run(capsys, "set", "--cpu", "45")
    assert code == 0
    assert "refused" not in out


def test_rapl_readback_mismatch_is_refused(env, capsys, monkeypatch):
    real = pc.write_sysfs

    def clamp(path, value):     # firmware keeps PL1 at 91 W
        if Path(path).name == "constraint_0_power_limit_uw":
            value = 91000000
        return real(path, value)

    monkeypatch.setattr(pc, "write_sysfs", clamp)
    code, out, _ = run(capsys, "set", "--cpu", "45")
    assert code == 1
    assert "constraint_0 45 W: refused by hardware/driver (wrote 45 W, reads back 91 W)" in out
    assert "constraint_1 45 W: applied" in out


def test_gpu_accepted(env, capsys):
    code, out, _ = run(capsys, "set", "--gpu", "120")
    assert code == 0
    assert "GPU 120 W: applied" in out
    assert env.read("constraint_0_power_limit_uw") == "91000000"


def test_gpu_refused(env, capsys):
    env.set_gpu(mode="refuse")
    code, out, _ = run(capsys, "set", "--safe")
    assert code == 1
    assert "GPU 95 W: refused by hardware/driver (wrote 95 W, reads back 190 W)" in out
    assert env.read("constraint_0_power_limit_uw") == "46000000"   # CPU still capped


def test_gpu_nvidia_smi_error(env, capsys):
    env.set_gpu(mode="error")
    code, out, _ = run(capsys, "set", "--gpu", "80")
    assert code == 1
    assert ("GPU 80 W: nvidia-smi error: Changing power management limit is not supported "
            "for GPU: 00000000:01:00.0.") in out


def test_gpu_missing_binary_is_reported(env, capsys, monkeypatch):
    monkeypatch.setenv("POWER_WATCH_NVIDIA_SMI", str(env.smi.parent / "missing-nvidia-smi"))
    code, out, _ = run(capsys, "set", "--gpu", "80")
    assert code == 1
    assert "nvidia-smi error: command not found" in out
    assert "`reset` will not be able to restore it" in out


def test_disabled_zone_is_enabled_then_restored(env, capsys):
    env.write("enabled", "0")
    code, out, _ = run(capsys, "set", "--cpu", "45")
    assert code == 0
    assert env.read("enabled") == "1"
    assert "intel-rapl:0 enabled: applied" in out
    assert env.originals()["zones"]["intel-rapl:0"]["enabled"] == 0
    assert run(capsys, "reset")[0] == 0
    assert env.read("enabled") == "0"
    assert env.read("constraint_0_power_limit_uw") == "91000000"


def test_permission_error_needs_root(env, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    (env.zone / "constraint_0_power_limit_uw").chmod(0o444)
    code, _, err = run(capsys, "set", "--cpu", "45")
    assert code == 1
    assert "needs root: run sudo power-cap set --cpu 45" in err
    assert not (env.state / "originals.json").exists()   # nothing changed, nothing saved


def test_permission_error_on_state_dir_needs_root(env, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    env.state.parent.mkdir(parents=True)
    env.state.parent.chmod(0o555)
    try:
        code, _, err = run(capsys, "set", "--safe")
    finally:
        env.state.parent.chmod(0o755)
    assert code == 1
    assert "needs root: run sudo power-cap set --safe" in err
    assert env.read("constraint_0_power_limit_uw") == "91000000"


def test_permission_error_as_root_is_refused_and_set_continues(env, capsys, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("real root ignores file modes")
    monkeypatch.setattr(pc, "geteuid", lambda: 0)
    (env.zone / "constraint_0_power_limit_uw").chmod(0o444)   # like a locked PL1
    code, out, err = run(capsys, "set", "--safe")
    assert code == 1
    assert "needs root" not in err
    assert ("constraint_0 46 W: refused (permission denied as root — power limit likely "
            "locked by firmware)") in out
    assert "constraint_1 46 W: applied" in out
    assert env.read("constraint_1_power_limit_uw") == "46000000"
    assert "GPU 95 W: applied" in out and env.gpu_limit() == 95.0
    assert env.originals()["zones"]["intel-rapl:0"]["constraint_0_power_limit_uw"] == 91000000


def test_gpu_only_set_with_unknown_limit_writes_no_originals(env, capsys):
    env.set_gpu(limit="[N/A]")
    code, out, _ = run(capsys, "set", "--gpu", "80")
    assert code == 0 and "GPU 80 W: applied" in out
    assert "`reset` will not be able to restore it" in out
    assert not (env.state / "originals.json").exists()
    assert "no saved originals" in run(capsys, "status")[1]


# --- reset -----------------------------------------------------------------

def test_reset_as_root_reports_refused_and_restores_the_rest(env, capsys, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("real root ignores file modes")
    run(capsys, "set", "--safe")
    monkeypatch.setattr(pc, "geteuid", lambda: 0)
    (env.zone / "constraint_0_power_limit_uw").chmod(0o444)
    code, out, err = run(capsys, "reset")
    assert code == 1
    assert "needs root" not in err
    assert "constraint_0 91 W: refused (permission denied as root" in out
    assert "constraint_1 0 W: applied" in out
    assert env.read("constraint_1_power_limit_uw") == "0"
    assert env.gpu_limit() == 190.0
    assert (env.state / "originals.json").exists()


def test_reset_as_user_still_needs_root(env, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    run(capsys, "set", "--cpu", "45")
    (env.zone / "constraint_0_power_limit_uw").chmod(0o444)
    code, _, err = run(capsys, "reset")
    assert code == 1
    assert "needs root: run sudo power-cap reset" in err


@pytest.mark.parametrize("content", [{}, {"zones": {}}, {"zones": {"intel-rapl:0": {}}},
                                     {"gpu": {}}])
def test_reset_deletes_noop_originals(env, capsys, content):
    env.state.mkdir(parents=True)
    (env.state / "originals.json").write_text(json.dumps(content))
    assert "Cap: active" not in run(capsys, "status")[1]
    code, out, _ = run(capsys, "reset")
    assert code == 0
    assert "nothing to reset" in out
    assert not (env.state / "originals.json").exists()
    assert "no saved originals" in run(capsys, "status")[1]


def test_reset_restores_and_deletes_originals(env, capsys):
    run(capsys, "set", "--safe")
    code, out, _ = run(capsys, "reset")
    assert code == 0, out
    assert env.read("constraint_0_power_limit_uw") == "91000000"
    assert env.read("constraint_1_power_limit_uw") == "0"
    assert env.read("enabled") == "1"
    assert env.gpu_limit() == 190.0
    assert not (env.state / "originals.json").exists()
    assert "originals restored and cleared" in out


def test_reset_keeps_originals_when_gpu_restore_fails(env, capsys):
    run(capsys, "set", "--safe")
    env.set_gpu(mode="error")
    code, out, _ = run(capsys, "reset")
    assert code == 1
    assert "nvidia-smi error" in out
    assert (env.state / "originals.json").exists()
    assert env.read("constraint_0_power_limit_uw") == "91000000"


def test_reset_with_nothing_saved(env, capsys):
    code, out, _ = run(capsys, "reset")
    assert code == 0
    assert "nothing to reset" in out


def test_corrupt_originals_is_an_error(env, capsys):
    env.state.mkdir(parents=True)
    (env.state / "originals.json").write_text("{not json")
    code, _, err = run(capsys, "reset")
    assert code == 1
    assert "unreadable saved originals" in err


def test_cli_set_and_reset_via_subprocess(env):
    e = {**os.environ, **env.vars}
    r = subprocess.run([str(SCRIPT), "set", "--safe"], capture_output=True, text=True, env=e)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run([str(SCRIPT), "reset"], capture_output=True, text=True, env=e)
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.read("constraint_0_power_limit_uw") == "91000000"


def test_help_describes_configured_safe_caps_without_fixed_watts():
    for argv in (["set", "--help"], ["--help"]):
        r = subprocess.run([str(SCRIPT), *argv], capture_output=True, text=True, check=False)
        assert r.returncode == 0
        text = " ".join(r.stdout.split())        # argparse wraps lines
        assert "configured safe caps (default: half the original limits)" in text
        assert "45 W" not in text and "80 W" not in text
    r = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
    assert "--config PATH" in r.stdout


# --- safe caps and config ----------------------------------------------------

def test_safe_derived_from_current_limits(env, capsys):
    code, out, _ = run(capsys, "set", "--safe")
    assert code == 0, out
    assert env.read("constraint_0_power_limit_uw") == "46000000"   # 91 W / 2, half-up
    assert env.read("constraint_1_power_limit_uw") == "46000000"
    assert "-i 0 -pl 95" in env.smi_calls()                         # 190 W / 2


def test_second_safe_derives_from_originals_not_halving_again(env, capsys):
    assert run(capsys, "set", "--safe")[0] == 0
    code, out, _ = run(capsys, "set", "--safe")
    assert code == 0, out
    assert env.read("constraint_0_power_limit_uw") == "46000000"
    assert env.gpu_limit() == 95.0
    assert env.originals()["gpu"] == {"enforced_limit_w": 190.0}


def test_safe_derived_from_originals_after_manual_cap(env, capsys):
    run(capsys, "set", "--cpu", "20", "--gpu", "30")
    code, out, _ = run(capsys, "set", "--safe")
    assert code == 0, out
    assert env.read("constraint_0_power_limit_uw") == "46000000"   # from the saved 91 W
    assert env.gpu_limit() == 95.0                                  # from the saved 190 W


def test_safe_cpu_floor_is_min_cpu_w(env, capsys):
    env.write("constraint_0_power_limit_uw", "6000000")
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "5000000"


def test_safe_gpu_derived_is_clamped_to_min_limit(env, capsys):
    env.set_gpu(limit="1.50")                   # half = 1 W (half-up of 0.75), min 1 W
    assert run(capsys, "set", "--safe")[0] == 0
    assert "-i 0 -pl 1" in env.smi_calls()


def test_safe_cpu_underivable_when_pl1_reads_zero(env, capsys):
    env.write("constraint_0_power_limit_uw", "0")
    code, _, err = run(capsys, "set", "--safe")
    assert code == 2
    assert "cannot derive a safe CPU cap for intel-rapl:0" in err and "safe_cpu_w" in err
    assert not (env.state / "originals.json").exists()


def test_safe_without_gpu_data_skips_the_gpu(env, capsys, monkeypatch):
    monkeypatch.setenv("POWER_WATCH_NVIDIA_SMI", str(env.smi.parent / "missing-nvidia-smi"))
    code, out, _ = run(capsys, "set", "--safe")
    assert code == 0, out
    assert "GPU: skipped (no NVIDIA GPU limit known" in out
    assert env.read("constraint_0_power_limit_uw") == "46000000"
    assert "gpu" not in env.originals()


def test_configured_gpu_without_nvidia_smi_is_skipped(env, capsys, monkeypatch):
    env.config("[power-cap]\nsafe_gpu_w = 80\n")
    monkeypatch.setenv("POWER_WATCH_NVIDIA_SMI", str(env.smi.parent / "missing-nvidia-smi"))
    code, out, _ = run(capsys, "set", "--safe")
    assert code == 0, out
    assert "GPU: skipped (no NVIDIA GPU limit known; set [power-cap] safe_gpu_w" in out
    assert env.read("constraint_0_power_limit_uw") == "46000000"
    assert "gpu" not in env.originals()


def test_configured_gpu_without_nvidia_device_is_skipped_without_nvidia_smi(env, capsys):
    shutil.rmtree(env.root / "bus" / "pci" / "devices")
    (env.root / "bus" / "pci" / "devices").mkdir()
    env.pci("0000:00:02.0", "0x030000", "active", vendor="0x8086")     # Intel iGPU only
    env.config("[power-cap]\nsafe_gpu_w = 80\n")
    code, out, _ = run(capsys, "set", "--safe")
    assert code == 0, out
    assert "GPU: skipped (no NVIDIA GPU limit known" in out
    assert env.smi_calls() == []
    out = run(capsys, "status")[1]
    assert "n/a (no NVIDIA GPU under" in out and "GPU skipped (no NVIDIA GPU limit known)" in out
    assert env.smi_calls() == []
    code, out, _ = run(capsys, "set", "--gpu", "80")      # explicit --gpu still errors
    assert code == 1 and "GPU 80 W: no NVIDIA GPU under" in out


def test_explicit_gpu_without_nvidia_smi_still_fails(env, capsys, monkeypatch):
    monkeypatch.setenv("POWER_WATCH_NVIDIA_SMI", str(env.smi.parent / "missing-nvidia-smi"))
    code, out, _ = run(capsys, "set", "--gpu", "80")
    assert code == 1
    assert "GPU 80 W: nvidia-smi error: command not found" in out


# --- runtime-suspended GPU: never woken --------------------------------------

@pytest.mark.parametrize("runtime", ["suspended", "suspending"])
def test_status_does_not_query_a_suspended_gpu(env, capsys, runtime):
    env.gpu_runtime(runtime)
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "GPU (nvidia-smi)\n  suspended, not queried" in out
    assert "GPU skipped (runtime-suspended, not woken)" in out
    assert env.smi_calls() == []


@pytest.mark.parametrize("argv, label", [(["--safe"], "GPU"), (["--gpu", "80"], "GPU 80 W"),
                                         (["--cpu", "50", "--gpu", "80"], "GPU 80 W")])
def test_set_skips_a_suspended_gpu(env, capsys, argv, label):
    env.config("[power-cap]\nsafe_gpu_w = 80\n")
    env.gpu_runtime("suspended")
    code, out, _ = run(capsys, "set", *argv)
    assert code == 0, out
    assert f"{label}: skipped (NVIDIA GPU runtime-suspended; not woken to cap it)" in out
    assert env.smi_calls() == []
    assert env.gpu_limit() == 190.0
    assert "gpu" not in (env.originals() if (env.state / "originals.json").exists() else {})


def test_reset_keeps_gpu_original_while_suspended(env, capsys):
    assert run(capsys, "set", "--gpu", "80")[0] == 0
    env.gpu_runtime("suspended")
    before = len(env.smi_calls())
    code, out, _ = run(capsys, "reset")
    assert code == 1
    assert "GPU 190 W: not restored: NVIDIA GPU runtime-suspended" in out
    assert len(env.smi_calls()) == before
    assert env.originals()["gpu"]["enforced_limit_w"] == 190.0
    env.gpu_runtime("active")
    assert run(capsys, "reset")[0] == 0
    assert env.gpu_limit() == 190.0


def test_gpu_state_uses_the_nvidia_display_function(env):
    assert pc.gpu_state() == "active"          # the suspended 01:00.1 is audio, not display
    env.gpu_runtime("suspending")
    assert pc.gpu_state() == "suspended"


def test_unreadable_pci_list_still_queries(env, capsys):
    shutil.rmtree(env.root / "bus")
    assert pc.gpu_state() == "unknown"
    out = run(capsys, "status")[1]
    assert "draw 36.7 W" in out


def test_configured_safe_values_out_of_range_are_usage_errors(env, capsys):
    env.config("[power-cap]\nsafe_gpu_w = 500\n")
    code, _, err = run(capsys, "set", "--safe")
    assert code == 2 and "GPU limit 500 W out of range" in err


def test_status_shows_resolved_safe_caps(env, capsys):
    out = run(capsys, "status")[1]
    assert ("Safe caps (set --safe): CPU 46 W (half the original PL1); "
            "GPU 95 W (half the original limit)") in out
    path = env.config("[power-cap]\nsafe_cpu_w = 45\nsafe_gpu_w = 80\n")
    out = run(capsys, "status")[1]
    assert f"CPU 45 W (configured in {path}); GPU 80 W (configured in {path})" in out


def test_status_safe_caps_unknown_when_originals_unreadable(env, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    run(capsys, "set", "--cpu", "50")
    env.state.chmod(0o000)
    try:
        out = run(capsys, "status")[1]
    finally:
        env.state.chmod(0o700)
    assert "CPU unknown (saved originals not readable" in out


def test_config_other_sections_ignored_and_inline_comments(env, capsys):
    env.config("[adapter]\nvoltage_v = 19.5\nbogus = x\n\n[power-cap]\n"
               "safe_cpu_w = 40   # watts\n")
    code, _, err = run(capsys, "set", "--safe")
    assert code == 0
    assert env.read("constraint_0_power_limit_uw") == "40000000"
    assert err == ""                            # [adapter] keys are not power-cap's business


def test_config_unknown_key_warns(env, capsys):
    env.config("[power-cap]\nsafe_cpu_w = 40\nsafe_ram_w = 3\n")
    code, _, err = run(capsys, "set", "--safe")
    assert code == 0
    assert "warning:" in err and "[power-cap] safe_ram_w: unknown key, ignored" in err


def test_config_empty_value_means_default(env, capsys):
    env.config("[power-cap]\nsafe_cpu_w =\n")
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "46000000"


def test_config_flag_wins_over_env_and_xdg(env, capsys, monkeypatch, tmp_path):
    env.config("[power-cap]\nsafe_cpu_w = 30\n")
    monkeypatch.setenv("POWER_WATCH_CONFIG", str(env.config("[power-cap]\nsafe_cpu_w = 35\n",
                                                            tmp_path / "env.ini")))
    flag = env.config("[power-cap]\nsafe_cpu_w = 40\n", tmp_path / "flag.ini")
    assert run(capsys, "--config", str(flag), "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "40000000"
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "35000000"


@pytest.mark.parametrize("how", ["flag", "env"])
def test_named_config_missing_is_exit_2(env, capsys, monkeypatch, tmp_path, how):
    missing = tmp_path / "nope.ini"
    argv = ["status"]
    if how == "flag":
        argv = ["--config", str(missing), *argv]
    else:
        monkeypatch.setenv("POWER_WATCH_CONFIG", str(missing))
    code, out, err = run(capsys, *argv)
    assert code == 2 and out == ""
    assert f"{missing}: config file not found" in err


SECRET = "hunter2-do-not-echo"


@pytest.mark.parametrize("text, msg", [
    (f"[power-cap]\nsafe_cpu_w = {SECRET}\n", "[power-cap] safe_cpu_w: must be a positive number"),
    ("[power-cap]\nsafe_gpu_w = -5\n", "[power-cap] safe_gpu_w: must be a positive number"),
    ("[power-cap]\nsafe_gpu_w = 0\n", "[power-cap] safe_gpu_w: must be a positive number"),
    ("[power-cap]\nsafe_gpu_w = nan\n", "[power-cap] safe_gpu_w: must be a positive number"),
    (f"{SECRET}\n[power-cap]\n", "parse error at line 1"),
    (f"[power-cap]\nsafe_cpu_w = 40\n{SECRET}\n", "parse error at line 3"),
    ("[power-cap]\nsafe_cpu_w = 40\nsafe_cpu_w = 41\n", "parse error at line 3"),
])
def test_bad_config_is_exit_2_without_content(env, capsys, text, msg):
    path = env.config(text)
    code, out, err = run(capsys, "set", "--safe")
    assert code == 2
    assert f"{path}" in err and msg in err
    assert SECRET not in out + err and "-5" not in err
    assert env.read("constraint_0_power_limit_uw") == "91000000"
    assert not (env.state / "originals.json").exists()


def test_bad_config_does_not_block_reset(env, capsys):
    assert run(capsys, "set", "--cpu", "50")[0] == 0
    env.config("[power-cap]\nsafe_cpu_w = x\n")
    code, out, _ = run(capsys, "reset")
    assert code == 0, out
    assert env.read("constraint_0_power_limit_uw") == "91000000"


def test_sudo_user_config_is_read_as_root(env, capsys, monkeypatch, tmp_path):
    home = tmp_path / "home-alice"
    env.config("[power-cap]\nsafe_cpu_w = 42\n", home / ".config/power-watch/config")
    env.config("[power-cap]\nsafe_cpu_w = 30\n")        # root's own XDG file comes after
    monkeypatch.setenv("SUDO_USER", "alice")
    ids = FakeIds(monkeypatch)
    looked_up = []

    def getpwnam(name):
        looked_up.append(name)
        return alice(home)(name)

    monkeypatch.setattr(pc.pwd, "getpwnam", getpwnam)
    assert run(capsys, "set", "--safe")[0] == 0
    assert looked_up == ["alice"]
    assert env.read("constraint_0_power_limit_uw") == "42000000"
    user_file = str(home / ".config/power-watch/config")
    assert [o[:4] for o in ids.opens if o[0] == user_file] == [(user_file, 1000, 1000, [1000, 1500])]


def test_sudo_user_without_file_falls_back_to_xdg(env, capsys, monkeypatch, tmp_path):
    env.config("[power-cap]\nsafe_cpu_w = 30\n")
    monkeypatch.setenv("SUDO_USER", "alice")
    FakeIds(monkeypatch)
    monkeypatch.setattr(pc.pwd, "getpwnam", alice(tmp_path / "empty"))
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "30000000"


def test_sudo_user_unknown_or_not_root_is_ignored(env, capsys, monkeypatch, tmp_path):
    home = tmp_path / "home-alice"
    env.config("[power-cap]\nsafe_cpu_w = 42\n", home / ".config/power-watch/config")
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(pc.pwd, "getpwnam", alice(home))
    # not root: SUDO_USER is not consulted
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "46000000"

    def unknown(name):
        raise KeyError(name)

    FakeIds(monkeypatch)
    monkeypatch.setattr(pc.pwd, "getpwnam", unknown)
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "46000000"


def test_doas_user_config_is_read_and_sudo_user_wins(env, capsys, monkeypatch, tmp_path):
    homes = {"alice": tmp_path / "home-alice", "bob": tmp_path / "home-bob"}
    env.config("[power-cap]\nsafe_cpu_w = 42\n", homes["alice"] / ".config/power-watch/config")
    env.config("[power-cap]\nsafe_cpu_w = 44\n", homes["bob"] / ".config/power-watch/config")
    ids = FakeIds(monkeypatch)
    uids = {"alice": 1000, "bob": 1001}
    monkeypatch.setattr(pc.pwd, "getpwnam",
                        lambda n: alice(homes[n], uid=uids[n], gid=uids[n] + 100)(n))
    monkeypatch.setenv("DOAS_USER", "bob")
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "44000000"
    assert ("seteuid", 1001) in ids.calls and ("setegid", 1101) in ids.calls
    monkeypatch.setenv("SUDO_USER", "alice")
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "42000000"


# --- reading a user's config as root --------------------------------------------

def test_named_config_is_opened_as_the_sudo_user_and_root_restored(env, capsys, monkeypatch,
                                                                   tmp_path, root_ids):
    path = env.config("[power-cap]\nsafe_cpu_w = 40\n", tmp_path / "mine.ini")
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("SUDO_UID", "1234")
    monkeypatch.setenv("SUDO_GID", "5678")
    monkeypatch.setattr(pc.pwd, "getpwnam", alice(tmp_path, uid=1, gid=1))  # SUDO_* ids win
    assert run(capsys, "--config", str(path), "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "40000000"
    assert [o[:4] for o in root_ids.opens if o[0] == str(path)] == [
        (str(path), 1234, 5678, [5678, 1500])]
    # drop: groups, gid, uid; restore: uid, gid, groups
    assert root_ids.calls == [("setgroups", [5678, 1500]), ("setegid", 5678), ("seteuid", 1234),
                              ("seteuid", 0), ("setegid", 0), ("setgroups", [0])]
    flags = next(o[4] for o in root_ids.opens if o[0] == str(path))
    assert flags & os.O_NONBLOCK


def test_env_config_opened_as_user_restores_root_when_missing(env, capsys, monkeypatch, tmp_path,
                                                              root_ids):
    missing = tmp_path / "nope.ini"
    monkeypatch.setenv("POWER_WATCH_CONFIG", str(missing))
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(pc.pwd, "getpwnam", alice(tmp_path))
    code, _, err = run(capsys, "status")
    assert code == 2 and f"{missing}: config file not found" in err
    assert (root_ids.euid, root_ids.egid, root_ids.groups) == (0, 0, [0])
    assert [c[0] for c in root_ids.calls] == ["setgroups", "setegid", "seteuid",
                                              "seteuid", "setegid", "setgroups"]


def test_failed_identity_drop_is_undone_and_reported(env, capsys, monkeypatch, tmp_path,
                                                     root_ids):
    path = env.config("[power-cap]\nsafe_cpu_w = 40\n", tmp_path / "mine.ini")
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(pc.pwd, "getpwnam", alice(tmp_path))

    def refuse(uid):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(pc, "seteuid", refuse)
    code, _, err = run(capsys, "--config", str(path), "status")
    assert code == 2 and "cannot switch to user alice" in err
    assert (root_ids.egid, root_ids.groups) == (0, [0])
    assert all(o[0] != str(path) for o in root_ids.opens)


def test_root_without_invoking_user_reads_named_config_as_root(env, capsys, tmp_path, root_ids):
    path = env.config("[power-cap]\nsafe_cpu_w = 40\n", tmp_path / "mine.ini")
    assert run(capsys, "--config", str(path), "set", "--safe")[0] == 0
    assert root_ids.calls == []
    assert env.read("constraint_0_power_limit_uw") == "40000000"


def test_unresolvable_doas_user_refuses_named_config(env, capsys, monkeypatch, tmp_path, root_ids):
    path = env.config("[power-cap]\nsafe_cpu_w = 40\n", tmp_path / "mine.ini")
    monkeypatch.setenv("DOAS_USER", "ghost")

    def unknown(name):
        raise KeyError(name)

    monkeypatch.setattr(pc.pwd, "getpwnam", unknown)
    code, _, err = run(capsys, "--config", str(path), "status")
    assert code == 2 and "cannot resolve invoking user ghost" in err
    assert all(o[0] != str(path) for o in root_ids.opens)


def test_fifo_config_is_not_a_regular_file(env, capsys, tmp_path):
    fifo = tmp_path / "fifo.ini"
    os.mkfifo(fifo)                              # a blocking open would hang here
    code, out, err = run(capsys, "--config", str(fifo), "status")
    assert code == 2 and out == ""
    assert f"{fifo}: not a regular file" in err


def test_directory_config_is_not_a_regular_file(env, capsys, tmp_path):
    code, _, err = run(capsys, "--config", str(tmp_path), "status")
    assert code == 2 and f"{tmp_path}: not a regular file" in err


def test_oversized_config_is_refused_without_content(env, capsys, tmp_path):
    path = env.config("[power-cap]\nsafe_cpu_w = 40\nsecret_key = " + SECRET
                      + "\n" + "#" * (64 * 1024), tmp_path / "big.ini")
    code, out, err = run(capsys, "--config", str(path), "set", "--safe")
    assert code == 2 and out == ""
    assert f"{path}: too large" in err
    assert SECRET not in err and "secret_key" not in err
    assert env.read("constraint_0_power_limit_uw") == "91000000"


def test_config_of_exactly_64_kib_is_read(env, capsys, tmp_path):
    head = "[power-cap]\nsafe_cpu_w = 40\n"
    path = env.config(head + "#" * (64 * 1024 - len(head)), tmp_path / "edge.ini")
    assert path.stat().st_size == 64 * 1024
    assert run(capsys, "--config", str(path), "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "40000000"


# --- config parsing parity with power-watch ------------------------------------

def test_default_section_is_an_ignored_unknown_section(env, capsys):
    env.config("[DEFAULT]\nsafe_cpu_w = 30\nbogus = 1\n\n[power-cap]\nsafe_gpu_w = 80\n")
    code, out, err = run(capsys, "set", "--safe")
    assert code == 0, out
    assert err == ""                             # no false unknown-key warning for bogus
    assert env.read("constraint_0_power_limit_uw") == "46000000"   # 30 W not copied in
    assert env.gpu_limit() == 80.0


def test_config_example_loads_cleanly(env, capsys):
    env.config((SCRIPT.parent.parent / "config.example").read_text())
    code, out, err = run(capsys, "status")
    assert code == 0 and err == ""
    assert "CPU 46 W (half the original PL1)" in out


def test_relative_xdg_config_home_is_ignored(env, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env.config("[power-cap]\nsafe_cpu_w = 30\n", tmp_path / "rel" / "power-watch" / "config")
    monkeypatch.setenv("XDG_CONFIG_HOME", "rel")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env.config("[power-cap]\nsafe_cpu_w = 35\n", tmp_path / "home/.config/power-watch/config")
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "35000000"


def test_safe_uses_every_package_zone(env, capsys):
    z1 = env.root / "class" / "powercap" / "intel-rapl:1"
    z1.mkdir()
    for name, value in (("name", "package-1"), ("enabled", "1"),
                        ("constraint_0_power_limit_uw", "60000000"),
                        ("constraint_1_power_limit_uw", "70000000")):
        (z1 / name).write_text(f"{value}\n")
    out = run(capsys, "status")[1]
    assert "CPU intel-rapl:0 46 W, intel-rapl:1 30 W (half the original PL1)" in out
    assert run(capsys, "set", "--safe")[0] == 0
    assert env.read("constraint_0_power_limit_uw") == "46000000"
    assert (z1 / "constraint_0_power_limit_uw").read_text().strip() == "30000000"
    assert env.originals()["zones"]["intel-rapl:1"]["constraint_0_power_limit_uw"] == 60000000
    assert run(capsys, "reset")[0] == 0
    assert (z1 / "constraint_1_power_limit_uw").read_text().strip() == "70000000"


@pytest.mark.parametrize("raw", ["1099511627776", "-1", "4294967295", "abc"])
def test_env_int_rejects_out_of_range_ids(monkeypatch, raw):
    # an out-of-range SUDO_UID would raise OverflowError (not OSError) in seteuid
    monkeypatch.setenv("SUDO_UID", raw)
    assert pc._env_int("SUDO_UID") is None


def test_env_int_accepts_valid_id(monkeypatch):
    monkeypatch.setenv("SUDO_UID", "1000")
    assert pc._env_int("SUDO_UID") == 1000


# --- thermald: warning, --pause-thermald, reset ---------------------------------

STOP = ["--no-ask-password", "stop", "thermald.service"]
START = ["--no-ask-password", "start", "thermald.service"]
WARNING = ("warning: thermald.service is active and may restore the RAPL limits; "
           "add --pause-thermald to stop it until `reset`")


@pytest.fixture
def as_root(monkeypatch):
    """Pretend to be root where power-cap checks it (no identity switch happens here)."""
    monkeypatch.setattr(pc, "geteuid", lambda: 0)


@pytest.mark.parametrize("argv", [["--safe"], ["--cpu", "45"]])
def test_set_warns_when_thermald_is_active_and_never_stops_it(env, capsys, argv):
    env.thermald("active")
    code, out, _ = run(capsys, "set", *argv)
    assert code == 0, out
    assert out.count("warning:") == 1 and WARNING in out
    assert env.sc_calls() == [["is-active", "thermald.service"]]
    assert env.thermald() == "active"
    assert "thermald_stopped" not in env.originals()


def test_set_without_thermald_or_cpu_asks_nothing(env, capsys):
    assert "warning" not in run(capsys, "set", "--cpu", "45")[1]       # inactive: no warning
    before = len(env.sc_calls())
    env.thermald("active")
    code, out, _ = run(capsys, "set", "--gpu", "80")                  # GPU only: not RAPL
    assert code == 0 and "warning" not in out
    assert len(env.sc_calls()) == before


@pytest.mark.parametrize("argv", [["--safe"], ["--cpu", "45", "--gpu", "80"]])
def test_pause_thermald_stops_it_before_any_rapl_write(env, capsys, as_root, argv):
    env.thermald("active")
    code, out, _ = run(capsys, "set", *argv, "--pause-thermald")
    assert code == 0, out
    assert "warning" not in out
    assert env.sc_verbs() == [STOP]
    assert env.sc_calls() == [["is-active", "thermald.service"], STOP,
                              ["is-active", "thermald.service"]]
    assert (env.sc_state / "pl1_at_stop").read_text().split() == ["91000000"]   # not yet written
    assert env.read("constraint_0_power_limit_uw") != "91000000"
    assert "  thermald.service: stopped (reset starts it again)" in out
    assert out.index("thermald.service: stopped") < out.index("constraint_0")
    assert env.originals()["thermald_stopped"] is True
    assert env.originals()["zones"]["intel-rapl:0"]["constraint_0_power_limit_uw"] == 91000000
    assert stat.S_IMODE((env.state / "originals.json").stat().st_mode) == 0o600


def test_pause_thermald_marker_survives_a_second_set_and_reset_starts_it(env, capsys, as_root):
    env.thermald("active")
    assert run(capsys, "set", "--safe", "--pause-thermald")[0] == 0
    code, out, _ = run(capsys, "set", "--cpu", "40", "--pause-thermald")
    assert code == 0 and "nothing to pause (stopped by power-cap earlier)" in out
    assert env.originals()["thermald_stopped"] is True
    assert env.sc_verbs() == [STOP]
    code, out, _ = run(capsys, "reset")
    assert code == 0, out
    assert "  thermald.service: started again" in out
    assert env.sc_verbs() == [STOP, START]
    assert env.thermald() == "active"
    assert not (env.state / "originals.json").exists()
    assert env.read("constraint_0_power_limit_uw") == "91000000"


def test_pause_thermald_when_inactive_writes_no_marker_and_reset_never_starts(env, capsys):
    code, out, _ = run(capsys, "set", "--cpu", "45", "--pause-thermald")    # no root needed
    assert code == 0, out
    assert "  thermald.service: not active, nothing to pause" in out
    assert "thermald_stopped" not in env.originals()
    code, out, _ = run(capsys, "reset")
    assert code == 0 and "thermald" not in out
    assert env.sc_verbs() == []
    assert env.thermald() == "inactive"


def test_reset_never_starts_a_thermald_power_cap_did_not_stop(env, capsys):
    env.thermald("active")
    assert run(capsys, "set", "--cpu", "45")[0] == 0                         # warned only
    env.thermald("inactive")                                                 # stopped by hand
    assert run(capsys, "reset")[0] == 0
    assert env.sc_verbs() == [] and env.thermald() == "inactive"


def test_pause_thermald_as_user_needs_root_and_changes_nothing(env, capsys):
    if os.geteuid() == 0:
        pytest.skip("real root")
    env.thermald("active")
    code, _, err = run(capsys, "set", "--safe", "--pause-thermald")
    assert code == 1
    assert ("power-cap: needs root: run sudo power-cap set --safe --pause-thermald "
            "(stopping thermald.service needs root)") in err
    assert env.sc_verbs() == []
    assert env.read("constraint_0_power_limit_uw") == "91000000"
    assert not (env.state / "originals.json").exists()


@pytest.mark.parametrize("flag, msg", [
    ("stop_fail", "not stopped: systemctl error: Failed to stop thermald.service: Access denied"),
    ("stop_noop", "not stopped: still active after systemctl stop"),
])
def test_failed_stop_is_a_failed_result_and_caps_still_apply(env, capsys, as_root, flag, msg):
    env.thermald("active")
    env.sc_flag(flag)
    code, out, _ = run(capsys, "set", "--cpu", "45", "--pause-thermald")
    assert code == 1
    assert f"  thermald.service: {msg}" in out
    assert env.read("constraint_0_power_limit_uw") == "45000000"
    assert "intel-rapl:0 constraint_0 45 W: applied" in out
    assert "thermald_stopped" not in env.originals()
    assert run(capsys, "reset")[0] == 0
    assert START not in env.sc_calls()


def test_failed_start_on_reset_keeps_originals(env, capsys, as_root):
    env.thermald("active")
    assert run(capsys, "set", "--cpu", "45", "--pause-thermald")[0] == 0
    env.sc_flag("start_fail")
    code, out, _ = run(capsys, "reset")
    assert code == 1
    assert "  thermald.service: not started: systemctl error: Job for thermald.service failed." in out
    assert env.read("constraint_0_power_limit_uw") == "91000000"         # limits still restored
    assert env.originals()["thermald_stopped"] is True
    (env.sc_state / "start_fail").unlink()
    assert run(capsys, "reset")[0] == 0
    assert env.thermald() == "active"


def test_marker_only_originals_are_resettable(env, capsys):
    env.state.mkdir(parents=True)
    (env.state / "originals.json").write_text('{"thermald_stopped": true}')
    assert pc.has_originals({"thermald_stopped": True})
    assert not pc.has_originals({"thermald_stopped": False})
    assert "Cap: active" in run(capsys, "status")[1]
    code, out, _ = run(capsys, "reset")
    assert code == 0, out
    assert env.sc_verbs() == [START]
    assert not (env.state / "originals.json").exists()


def test_missing_systemctl_is_unknown_without_warning_or_crash(env, capsys, monkeypatch, as_root):
    monkeypatch.setenv("POWER_CAP_SYSTEMCTL", str(env.systemctl.parent / "missing-systemctl"))
    assert pc.thermald_state() == "unknown"
    code, out, _ = run(capsys, "set", "--cpu", "45")
    assert code == 0 and "warning" not in out
    code, out, _ = run(capsys, "set", "--cpu", "45", "--pause-thermald")
    assert code == 0, out
    assert "thermald.service: state unknown (systemctl unavailable); nothing paused" in out
    assert "thermald_stopped" not in env.originals()
    assert "thermald: unknown" in run(capsys, "status")[1]


def test_systemctl_timeout_and_os_error_are_unknown(monkeypatch):
    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    def oserror(cmd, **kw):
        raise PermissionError(13, "Permission denied")

    for fake in (timeout, oserror):
        monkeypatch.setattr(pc.subprocess, "run", fake)
        assert pc.thermald_state() == "unknown"


@pytest.mark.parametrize("stdout, state", [("active\n", "active"), ("inactive\n", "inactive"),
                                           ("failed\n", "inactive"), ("activating\n", "inactive"),
                                           ("", "unknown")])
def test_thermald_state_from_is_active_output(monkeypatch, stdout, state):
    monkeypatch.setattr(pc.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=stdout, stderr=""))
    assert pc.thermald_state() == state


def test_systemctl_runs_as_an_argv_list_without_a_shell(monkeypatch):
    monkeypatch.setenv("POWER_CAP_SYSTEMCTL", "/usr/bin/systemctl --quiet-test")
    calls = []

    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout="inactive\n", stderr="")

    monkeypatch.setattr(pc.subprocess, "run", fake)
    assert pc.stop_thermald() == (True, "stopped (reset starts it again)")
    assert pc.start_thermald() == (True, "started again")
    assert [c for c, _ in calls] == [
        ["/usr/bin/systemctl", "--quiet-test", *STOP],
        ["/usr/bin/systemctl", "--quiet-test", "is-active", "thermald.service"],
        ["/usr/bin/systemctl", "--quiet-test", *START]]
    for cmd, kw in calls:
        assert isinstance(cmd, list) and "shell" not in kw and kw["capture_output"] is True
    assert [kw["timeout"] for _, kw in calls] == [30, 5, 30]
    assert not any(v in c for c, _ in calls for v in ("disable", "mask"))


def test_help_names_pause_thermald(env):
    r = subprocess.run([str(SCRIPT), "set", "--help"], capture_output=True, text=True, check=False)
    assert r.returncode == 0 and "--pause-thermald" in r.stdout


# --- applied record and drift ------------------------------------------------

def test_set_records_only_verified_cpu_limits(env, capsys, monkeypatch):
    real = pc.write_sysfs

    def clamp(path, value):     # firmware keeps PL1 at 91 W
        if Path(path).name == "constraint_0_power_limit_uw":
            value = 91000000
        return real(path, value)

    monkeypatch.setattr(pc, "write_sysfs", clamp)
    assert run(capsys, "set", "--cpu", "45")[0] == 1
    rec = env.applied()
    assert rec["zones"] == {"intel-rapl:0": {"constraint_1_power_limit_uw": 45000000}}
    assert isinstance(rec["time"], float)


def test_set_with_every_cpu_write_refused_on_fresh_state_records_none(env, capsys, monkeypatch):
    monkeypatch.setattr(pc, "write_sysfs", lambda p, v: "write failed: Input/output error")
    assert run(capsys, "set", "--cpu", "45")[0] == 1
    assert env.applied()["zones"] == {}
    assert "CPU cap: none\n" in run(capsys, "status")[1]
    from test_power_watch import pw
    assert pw.read_cpu_cap(env.root) == ("uncapped", None)


def test_set_with_every_cpu_write_refused_keeps_the_limits_still_in_place(env, capsys,
                                                                          monkeypatch):
    assert run(capsys, "set", "--cpu", "50")[0] == 0
    first = env.applied()["zones"]
    monkeypatch.setattr(pc, "write_sysfs", lambda p, v: "write failed: Input/output error")
    assert run(capsys, "set", "--cpu", "45")[0] == 1
    assert env.applied()["zones"] == first                      # the 50 W cap still holds
    assert "CPU cap: applied, live limits match" in run(capsys, "status")[1]


def test_set_with_all_cpu_writes_failing_opens_an_old_0700_dir(env, capsys, monkeypatch):
    env.state.mkdir(parents=True, mode=0o700)
    (env.state / "originals.json").write_text('{"gpu": {"enforced_limit_w": 190.0}}')
    monkeypatch.setattr(pc, "write_sysfs", lambda p, v: "write failed: Input/output error")
    assert run(capsys, "set", "--cpu", "45")[0] == 1
    assert stat.S_IMODE(env.state.stat().st_mode) == 0o755
    assert stat.S_IMODE((env.state / "applied.json").stat().st_mode) == 0o644


def test_gpu_only_set_opens_an_old_0700_dir(env, capsys):
    env.state.mkdir(parents=True, mode=0o700)
    (env.state / "originals.json").write_text('{"gpu": {"enforced_limit_w": 190.0}}')
    assert run(capsys, "set", "--gpu", "190")[0] == 0           # originals unchanged
    assert stat.S_IMODE(env.state.stat().st_mode) == 0o755


def test_failed_enable_leaves_the_zone_out_of_the_record(env, capsys, monkeypatch):
    env.write("enabled", "0")
    real = pc.write_sysfs
    monkeypatch.setattr(pc, "write_sysfs",
                        lambda p, v: None if Path(p).name == "enabled" else real(p, v))
    assert run(capsys, "set", "--cpu", "45")[0] == 1
    assert env.applied()["zones"] == {}


def test_gpu_only_set_on_fresh_state_records_no_cpu_cap(env, capsys):
    assert run(capsys, "set", "--gpu", "80")[0] == 0
    assert "gpu" in env.originals()
    assert env.applied()["zones"] == {}
    assert stat.S_IMODE((env.state / "applied.json").stat().st_mode) == 0o644
    assert "CPU cap: none\n" in run(capsys, "status")[1]
    from test_power_watch import pw
    assert pw.read_cpu_cap(env.root) == ("uncapped", None)
    assert run(capsys, "set", "--gpu", "70")[0] == 0           # a second one keeps it
    assert env.applied()["zones"] == {}
    assert run(capsys, "reset")[0] == 0
    assert not (env.state / "applied.json").exists()


def test_gpu_only_set_over_an_older_power_cap_cap_writes_no_record(env, capsys):
    env.state.mkdir(parents=True, mode=0o700)                    # an older power-cap's dir
    (env.state / "originals.json").write_text(json.dumps(
        {"zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": 91000000}},
         "gpu": {"enforced_limit_w": 190.0}}))
    assert run(capsys, "set", "--gpu", "80")[0] == 0              # writes no state file
    assert not (env.state / "applied.json").exists()            # that CPU cap stays unknown
    assert stat.S_IMODE(env.state.stat().st_mode) == 0o755      # yet the dir is opened
    assert pc.OLDER_CAP_LINE in run(capsys, "status")[1]


def test_gpu_only_set_keeps_the_cpu_record_and_a_cpu_set_replaces_it(env, capsys):
    assert run(capsys, "set", "--cpu", "50")[0] == 0
    first = env.applied()
    assert run(capsys, "set", "--gpu", "80")[0] == 0
    assert env.applied() == first
    assert run(capsys, "set", "--cpu", "45", "--cpu-pl2", "60")[0] == 0
    assert env.applied()["zones"] == {"intel-rapl:0": {"constraint_0_power_limit_uw": 45000000,
                                                       "constraint_1_power_limit_uw": 60000000}}


def test_reset_removes_the_record_also_with_nothing_to_reset(env, capsys):
    assert run(capsys, "set", "--cpu", "50")[0] == 0
    assert run(capsys, "reset")[0] == 0
    assert not (env.state / "applied.json").exists()
    (env.state / "applied.json").write_text('{"zones": {}}')        # stray record, no originals
    code, out, _ = run(capsys, "reset")
    assert code == 0 and "nothing to reset" in out
    assert not (env.state / "applied.json").exists()


def test_reset_with_gpu_restore_failing_after_the_cpu_restored_is_uncapped(env, capsys):
    from test_power_watch import pw
    assert run(capsys, "set", "--cpu", "45", "--gpu", "80")[0] == 0
    env.gpu_runtime("suspended")
    code, out, _ = run(capsys, "reset")
    assert code == 1 and "originals kept" in out
    assert env.read("constraint_0_power_limit_uw") == "91000000"
    assert env.applied()["zones"] == {}
    assert pw.read_cpu_cap(env.root) == ("uncapped", None)
    out = run(capsys, "status")[1]
    assert "CPU cap: none\n" in out and "Cap: active" in out
    env.gpu_runtime("active")
    assert run(capsys, "reset")[0] == 0
    assert not (env.state / "applied.json").exists()
    assert not (env.state / "originals.json").exists()


def test_reset_with_thermald_start_failing_after_the_cpu_restored_is_uncapped(env, capsys,
                                                                             as_root):
    from test_power_watch import pw
    env.thermald("active")
    assert run(capsys, "set", "--cpu", "45", "--pause-thermald")[0] == 0
    env.sc_flag("start_fail")
    assert run(capsys, "reset")[0] == 1
    assert env.applied()["zones"] == {}
    assert pw.read_cpu_cap(env.root) == ("uncapped", None)
    assert "CPU cap: none\n" in run(capsys, "status")[1]


def test_reset_with_a_cpu_restore_failing_keeps_that_limit_recorded(env, capsys, monkeypatch):
    from test_power_watch import pw
    assert run(capsys, "set", "--cpu", "45", "--cpu-pl2", "60")[0] == 0
    real = pc.write_sysfs
    monkeypatch.setattr(pc, "write_sysfs", lambda p, v: (
        "write failed: Input/output error" if Path(p).name == "constraint_0_power_limit_uw"
        else real(p, v)))
    code, out, _ = run(capsys, "reset")
    assert code == 1
    assert env.read("constraint_0_power_limit_uw") == "45000000"     # still capped
    assert env.read("constraint_1_power_limit_uw") == "0"            # restored
    assert env.applied()["zones"] == {"intel-rapl:0": {"constraint_0_power_limit_uw": 45000000}}
    assert pw.read_cpu_cap(env.root) == ("capped", 45.0)
    assert "CPU cap: applied, live limits match" in run(capsys, "status")[1]
    monkeypatch.setattr(pc, "write_sysfs", real)
    assert run(capsys, "reset")[0] == 0
    assert not (env.state / "applied.json").exists()
    assert pw.read_cpu_cap(env.root) == ("uncapped", None)


def test_reset_of_an_older_power_cap_cap(env, capsys, monkeypatch):
    """No record (an older power-cap): a CPU restore failing leaves it unknown, a GPU one alone
    records that the CPU is restored."""
    from test_power_watch import pw
    env.state.mkdir(parents=True)
    env.write("constraint_0_power_limit_uw", "45000000")
    (env.state / "originals.json").write_text(json.dumps(
        {"zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": 91000000}},
         "gpu": {"enforced_limit_w": 190.0}}))
    env.gpu_runtime("suspended")
    real = pc.write_sysfs
    monkeypatch.setattr(pc, "write_sysfs", lambda p, v: "write failed: Input/output error")
    assert run(capsys, "reset")[0] == 1
    assert not (env.state / "applied.json").exists()
    assert pw.read_cpu_cap(env.root) == (None, None)
    assert pc.OLDER_CAP_LINE in run(capsys, "status")[1]
    monkeypatch.setattr(pc, "write_sysfs", real)
    assert run(capsys, "reset")[0] == 1                             # GPU still suspended
    assert env.applied()["zones"] == {}
    assert pw.read_cpu_cap(env.root) == ("uncapped", None)


def test_status_cpu_cap_none_for_a_record_of_no_zones(env, capsys):
    env.state.mkdir(parents=True)
    (env.state / "applied.json").write_text('{"zones": {}, "time": 1.0}')
    (env.state / "originals.json").write_text('{"gpu": {"enforced_limit_w": 190.0}}')
    assert pc.load_applied() == {"zones": {}, "time": 1.0}
    assert "CPU cap: none\n" in run(capsys, "status")[1]


def test_status_cpu_cap_unknown_for_a_cap_by_an_older_power_cap(env, capsys):
    """originals.json without applied.json: only an older power-cap leaves that."""
    env.state.mkdir(parents=True)
    (env.state / "originals.json").write_text(json.dumps(
        {"zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": 91000000}}}))
    out = run(capsys, "status")[1]
    assert ("CPU cap: unknown (cap set by an older power-cap; `sudo power-cap reset` "
            "clears it)\n") in out


# The monitor's cpu_cap (bin/power-watch read_cpu_cap) and status's 'CPU cap:' line follow one
# rule (duplicated in both scripts); this table holds them together over the same fixtures.
STATUS_FOR_CPU_CAP = {"capped": "CPU cap: applied", "reverted": "CPU cap: reverted",
                      "uncapped": "CPU cap: none", None: "CPU cap: unknown"}


def _fixture(env, name):
    rec = {"zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": 45000000}}, "time": 1.0}
    env.state.mkdir(parents=True)
    if name == "no-state":
        pass
    elif name in ("capped", "reverted", "live-unreadable"):
        env.write("constraint_0_power_limit_uw", 45000000 if name != "reverted" else 91000000)
        if name == "live-unreadable":
            (env.zone / "constraint_0_power_limit_uw").unlink()
        (env.state / "applied.json").write_text(json.dumps(rec))
        (env.state / "originals.json").write_text("{}")
    elif name == "zone-gone":
        rec["zones"] = {"intel-rapl:5": rec["zones"]["intel-rapl:0"]}
        (env.state / "applied.json").write_text(json.dumps(rec))
    elif name in ("no-zones", "no-zones-with-originals"):
        (env.state / "applied.json").write_text('{"zones": {}}')
        if name == "no-zones-with-originals":
            (env.state / "originals.json").write_text("{}")
    elif name == "older-power-cap":
        (env.state / "originals.json").write_text("{}")
    elif name == "malformed":
        (env.state / "applied.json").write_text('{"zones": []}')
    elif name == "stray-record-no-originals":
        (env.state / "applied.json").write_text(json.dumps(rec))
        env.write("constraint_0_power_limit_uw", 45000000)


@pytest.mark.parametrize("name, cpu_cap", [
    ("no-state", "uncapped"), ("capped", "capped"), ("reverted", "reverted"),
    ("live-unreadable", None), ("zone-gone", "reverted"), ("no-zones", "uncapped"),
    ("no-zones-with-originals", "uncapped"), ("older-power-cap", None), ("malformed", None),
    ("stray-record-no-originals", "capped"),
])
def test_status_and_monitor_agree_on_the_cpu_cap(env, capsys, name, cpu_cap):
    from test_power_watch import pw
    _fixture(env, name)
    assert pw.read_cpu_cap(env.root)[0] == cpu_cap
    line = next(x for x in run(capsys, "status")[1].splitlines() if x.startswith("CPU cap:"))
    assert line.startswith(STATUS_FOR_CPU_CAP[cpu_cap]), line


def test_status_cpu_cap_applied_and_none(env, capsys):
    out = run(capsys, "status")[1]
    assert "CPU cap: none\n" in out and "thermald: inactive\n" in out
    assert run(capsys, "set", "--safe")[0] == 0
    out = run(capsys, "status")[1]
    assert "CPU cap: applied, live limits match\n" in out


def test_status_cpu_cap_within_tolerance_matches(env, capsys):
    assert run(capsys, "set", "--cpu", "45")[0] == 0
    env.write("constraint_0_power_limit_uw", "45999000")          # RAPL unit rounding
    assert "CPU cap: applied, live limits match" in run(capsys, "status")[1]


def test_status_cpu_cap_reverted_lists_each_mismatch(env, capsys):
    assert run(capsys, "set", "--cpu", "45")[0] == 0
    env.write("constraint_0_power_limit_uw", "91000000")          # as seen on nostromo
    out = run(capsys, "status")[1]
    assert ("CPU cap: reverted by something else — intel-rapl:0 constraint_0 applied 45 W, "
            "now 91 W\n") in out
    env.write("constraint_1_power_limit_uw", "0")
    env.write("enabled", "0")
    out = run(capsys, "status")[1]
    assert ("reverted by something else — intel-rapl:0 constraint_0 applied 45 W, now 91 W; "
            "intel-rapl:0 constraint_1 applied 45 W, now 0 W; intel-rapl:0 disabled") in out
    assert "--pause-thermald" not in out.split("CPU cap:")[1].splitlines()[0]


def test_status_reverted_with_thermald_active_hints_pause(env, capsys, as_root):
    assert run(capsys, "set", "--cpu", "45")[0] == 0
    env.write("constraint_0_power_limit_uw", "91000000")
    env.thermald("active")
    out = run(capsys, "status")[1]
    line = next(x for x in out.splitlines() if x.startswith("CPU cap:"))
    assert "reverted by something else" in line and "--pause-thermald" in line
    assert "thermald: active (may restore RAPL limits; see set --pause-thermald)" in out


def test_status_missing_zone_is_reverted(env, capsys):
    assert run(capsys, "set", "--cpu", "45")[0] == 0
    rec = env.applied()
    rec["zones"]["intel-rapl:1"] = {"constraint_0_power_limit_uw": 45000000}
    (env.state / "applied.json").write_text(json.dumps(rec))
    assert "CPU cap: reverted by something else — intel-rapl:1 gone" in run(capsys, "status")[1]


def test_status_live_limit_unreadable_is_unknown(env, capsys):
    assert run(capsys, "set", "--cpu", "45")[0] == 0
    (env.zone / "constraint_1_power_limit_uw").unlink()
    assert "CPU cap: unknown (live limits unreadable)" in run(capsys, "status")[1]


@pytest.mark.parametrize("content", [
    "{not json", "[]", '{"zones": []}',
    '{"zones": {"../etc": {"constraint_0_power_limit_uw": 1}}}',
    '{"zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": "45"}}}',
    '{"zones": {"intel-rapl:0": {"constraint_0_power_limit_uw": true}}}',
    '{"zones": {"intel-rapl:0": {"energy_uj": 1}}}', '{"zones": {"intel-rapl:0": {}}}',
    "{}" + " " * (64 * 1024),
])
def test_status_unreadable_record(env, capsys, content):
    env.state.mkdir(parents=True)
    (env.state / "applied.json").write_text(content)
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "CPU cap: unknown (record unreadable)" in out


def test_status_record_not_a_regular_file(env, capsys):
    env.state.mkdir(parents=True)
    os.mkfifo(env.state / "applied.json")       # a blocking open would hang here
    assert "CPU cap: unknown (record unreadable)" in run(capsys, "status")[1]


def test_status_record_permission_denied(env, capsys):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    assert run(capsys, "set", "--cpu", "45")[0] == 0
    (env.state / "applied.json").chmod(0o000)
    assert "CPU cap: unknown (record unreadable)" in run(capsys, "status")[1]


def test_status_as_user_reads_the_record_but_not_the_marker(env, capsys, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    monkeypatch.setattr(pc, "geteuid", lambda: 0)
    env.thermald("active")
    assert run(capsys, "set", "--cpu", "45", "--pause-thermald")[0] == 0
    monkeypatch.setattr(pc, "geteuid", lambda: 1000)
    out = run(capsys, "status")[1]
    assert "thermald: inactive (stopped by power-cap; reset starts it again)" in out
    (env.state / "originals.json").chmod(0o000)          # what a user sees of the 0600 file
    try:
        out = run(capsys, "status")[1]
    finally:
        (env.state / "originals.json").chmod(0o600)
    assert "CPU cap: applied, live limits match" in out
    assert "thermald: inactive\n" in out
    assert "Cap: active" in out


def test_power_watch_reads_the_record_power_cap_writes(env, capsys):
    """The drift rule is duplicated in bin/power-watch (read_cpu_cap): both scripts agree on
    the real record."""
    from test_power_watch import pw
    assert pw.read_cpu_cap(env.root) == ("uncapped", None)
    assert run(capsys, "set", "--safe")[0] == 0
    assert pw.read_cpu_cap(env.root) == ("capped", 46.0)
    env.write("constraint_0_power_limit_uw", "91000000")
    assert pw.read_cpu_cap(env.root) == ("reverted", 46.0)
    assert "reverted by something else" in run(capsys, "status")[1]
    assert run(capsys, "reset")[0] == 0
    assert pw.read_cpu_cap(env.root) == ("uncapped", None)
