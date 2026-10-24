"""Tests for install.sh into a DESTDIR staging root, plus the tmpfiles rule and user unit."""

import fnmatch
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SOURCES = ["install.sh", "bin/power-cap", "bin/power-watch", "tmpfiles/power-watch.conf",
           "systemd/power-watch.service", "config.example"]
INSTALLED = {
    "usr/local/bin/power-watch": ("bin/power-watch", 0o755),
    "usr/local/bin/power-cap": ("bin/power-cap", 0o755),
    "etc/tmpfiles.d/power-watch.conf": ("tmpfiles/power-watch.conf", 0o644),
    "usr/local/lib/systemd/user/power-watch.service": ("systemd/power-watch.service", 0o644),
    "usr/local/share/power-watch/config.example": ("config.example", 0o644),
}
CONF = "etc/tmpfiles.d/power-watch.conf"


def fake_getent(path, groups_file):
    """getent stub: `getent group NAME` succeeds iff NAME is a line of groups_file."""
    path.write_text('#!/bin/sh\n'
                    f'[ "$1" = group ] && [ -n "$2" ] && grep -qxF -- "$2" "{groups_file}"\n')
    path.chmod(0o755)
    return str(path)


def set_groups(env, *names):
    Path(env["GROUPS_FILE"]).write_text("".join(f"{n}\n" for n in names))


@pytest.fixture
def setup(tmp_path):
    """A copy of the repo sources (power-watch stubbed if not written yet), a tmpfiles
    recorder and a getent stub whose system has a wheel group (see set_groups)."""
    src = tmp_path / "src"
    for rel in SOURCES:
        dst = src / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if (REPO / rel).exists():
            shutil.copy2(REPO / rel, dst)
        else:
            dst.write_text("#!/bin/sh\necho stub\n")
    record = tmp_path / "tmpfiles-calls"
    recorder = tmp_path / "record-tmpfiles"
    recorder.write_text(f'#!/bin/sh\necho "$*" >> "{record}"\n')
    recorder.chmod(0o755)
    stage = tmp_path / "stage"
    groups = tmp_path / "groups"
    groups.write_text("wheel\n")
    env = {**os.environ, "DESTDIR": str(stage), "SYSTEMD_TMPFILES": str(recorder),
           "GETENT": fake_getent(tmp_path / "getent", groups), "GROUPS_FILE": str(groups),
           "HOME": str(tmp_path / "home")}
    for k in ("PREFIX", "RAPL_GROUP"):
        env.pop(k, None)
    return src, stage, record, env


def install(src, env, *args):
    return subprocess.run(["bash", str(src / "install.sh"), *args], capture_output=True,
                          text=True, env=env, check=False)


def test_install_files_modes_and_tmpfiles_call(setup):
    src, stage, record, env = setup
    r = install(src, env)
    assert r.returncode == 0, r.stderr
    for rel, (source, mode) in INSTALLED.items():
        path = stage / rel
        assert path.read_bytes() == (src / source).read_bytes(), rel
        assert stat.S_IMODE(path.stat().st_mode) == mode, rel
    assert record.read_text().splitlines() == [
        f"--create {stage}/etc/tmpfiles.d/power-watch.conf"]
    assert "power-watch calibrate" in r.stdout
    assert "./install.sh --user" in r.stdout


def test_install_honors_prefix(setup):
    src, stage, _, env = setup
    r = install(src, {**env, "PREFIX": "/opt/pw"})
    assert r.returncode == 0, r.stderr
    assert (stage / "opt/pw/bin/power-cap").exists()
    assert (stage / "opt/pw/lib/systemd/user/power-watch.service").exists()
    assert (stage / "etc/tmpfiles.d/power-watch.conf").exists()


def test_install_prefix_rewrites_unit_execstart_and_warns(setup):
    src, stage, _, env = setup
    repo_unit = (src / "systemd/power-watch.service").read_text()
    r = install(src, {**env, "PREFIX": "/opt/pw"})
    assert r.returncode == 0, r.stderr
    unit = stage / "opt/pw/lib/systemd/user/power-watch.service"
    text = unit.read_text()
    assert "ExecStart=/opt/pw/bin/power-watch --quiet\n" in text
    assert "/usr/local/bin" not in text
    assert "/opt/pw/share/power-watch/config.example" in text
    assert "/usr/local/share" not in text
    assert (stage / "opt/pw/share/power-watch/config.example").exists()
    assert stat.S_IMODE(unit.stat().st_mode) == 0o644
    assert not list(unit.parent.glob("*.tmp"))
    assert (src / "systemd/power-watch.service").read_text() == repo_unit   # source untouched
    assert "/opt/pw/lib/systemd/user may not be on the systemd user unit search path" in r.stderr


@pytest.mark.parametrize("prefix", [None, "/usr/local", "/usr"])
def test_standard_prefix_has_no_unit_path_warning(setup, prefix):
    src, stage, _, env = setup
    if prefix:
        env = {**env, "PREFIX": prefix}
    r = install(src, env)
    assert r.returncode == 0, r.stderr
    assert "warning" not in r.stderr
    unit = stage / (prefix or "/usr/local").lstrip("/") / "lib/systemd/user/power-watch.service"
    assert f"ExecStart={prefix or '/usr/local'}/bin/power-watch --quiet\n" in unit.read_text()


def test_uninstall_removes_files(setup):
    src, stage, _, env = setup
    assert install(src, env).returncode == 0
    r = install(src, env, "--uninstall")
    assert r.returncode == 0, r.stderr
    for rel in INSTALLED:
        assert not (stage / rel).exists(), rel
    assert not (stage / "usr/local/share/power-watch").exists()
    assert "reboot" in r.stdout


def test_uninstall_never_needs_a_group(setup):
    src, stage, _, env = setup
    assert install(src, env).returncode == 0
    set_groups(env)
    assert install(src, env, "--uninstall").returncode == 0
    assert not (stage / CONF).exists()


# ------------------------------------------------------------- tmpfiles group

def rule(stage):
    return [ln.split() for ln in (stage / CONF).read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]


def test_group_autodetects_wheel(setup):
    src, stage, _, env = setup
    set_groups(env, "users", "sudo", "wheel")
    r = install(src, env)
    assert r.returncode == 0, r.stderr
    assert rule(stage)[0][4] == "wheel"
    assert "RAPL energy counters group: wheel (auto-detected)" in r.stdout
    assert "readable by group wheel" in r.stdout


def test_group_falls_back_to_sudo(setup):
    src, stage, _, env = setup
    set_groups(env, "users", "sudo")
    r = install(src, env)
    assert r.returncode == 0, r.stderr
    assert rule(stage) == [["z", "/sys/class/powercap/intel-rapl:*/energy_uj", "0440", "root",
                            "sudo", "-", "-"]]
    assert "RAPL energy counters group: sudo (auto-detected)" in r.stdout
    assert "readable by group sudo" in r.stdout
    # only the rule changes; the comments still describe the default
    src_lines = (src / "tmpfiles/power-watch.conf").read_text().splitlines()
    got_lines = (stage / CONF).read_text().splitlines()
    assert [a for a, b in zip(src_lines, got_lines, strict=True) if a != b] == [src_lines[-1]]


@pytest.mark.parametrize("groups", [[], ["users"]])
def test_group_detection_failure_asks_for_group_and_writes_nothing(setup, groups):
    src, stage, record, env = setup
    set_groups(env, *groups)
    r = install(src, env)
    assert r.returncode == 1
    assert "neither a wheel nor a sudo group exists" in r.stderr
    assert "--group NAME" in r.stderr
    assert not stage.exists() and not record.exists()


def test_group_detection_without_getent_fails(setup, tmp_path):
    src, stage, _, env = setup
    missing = tmp_path / "no-getent"
    r = install(src, {**env, "GETENT": str(missing)})
    assert r.returncode == 1
    assert f"cannot detect groups: {missing} not found; pass --group NAME" in r.stderr
    assert "neither a wheel nor a sudo group" not in r.stderr
    assert not stage.exists()


def test_empty_rapl_group_env_is_unset(setup):
    src, _, _, env = setup
    r = install(src, {**env, "RAPL_GROUP": ""})
    assert r.returncode == 0, r.stderr
    assert "RAPL energy counters group: wheel (auto-detected)" in r.stdout


@pytest.mark.parametrize("args", [["--group", "power"], ["--group=power"]])
def test_group_flag_overrides_detection(setup, args):
    src, stage, _, env = setup
    set_groups(env, "wheel", "power")
    r = install(src, env, *args)
    assert r.returncode == 0, r.stderr
    assert rule(stage)[0][4] == "power"
    assert "RAPL energy counters group: power (--group)" in r.stdout


def test_group_env_and_flag_precedence(setup):
    src, stage, _, env = setup
    set_groups(env, "wheel", "power", "adm")
    r = install(src, {**env, "RAPL_GROUP": "power"})
    assert r.returncode == 0, r.stderr
    assert rule(stage)[0][4] == "power"
    assert "(RAPL_GROUP)" in r.stdout
    r = install(src, {**env, "RAPL_GROUP": "power"}, "--group", "adm")
    assert r.returncode == 0, r.stderr
    assert rule(stage)[0][4] == "adm"


def test_unknown_explicit_group_is_kept_with_warning_when_staging(setup):
    src, stage, _, env = setup
    r = install(src, env, "--group", "builders")
    assert r.returncode == 0, r.stderr
    assert "group builders does not exist here" in r.stderr
    assert rule(stage)[0][4] == "builders"


def test_unknown_explicit_group_fails_on_live_install(setup, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("would install into the real system as root")
    src, _, record, env = setup
    env.pop("DESTDIR")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "id").write_text("#!/bin/sh\necho 0\n")     # pass the root check only
    (fake_bin / "id").chmod(0o755)
    env.update(PATH=f"{fake_bin}:{env['PATH']}", PREFIX=str(tmp_path / "prefix"))
    r = install(src, env, "--group", "nosuchgroup")
    assert r.returncode == 1
    assert "group nosuchgroup (from --group) does not exist" in r.stderr
    assert not (tmp_path / "prefix").exists() and not record.exists()


@pytest.mark.parametrize("args, msg", [
    (["--group"], "--group needs a group name"),
    (["--group", ""], "--group needs a group name"),
    (["--group="], "--group needs a group name"),
    (["--group", "", "--uninstall"], "--group needs a group name"),
    (["--group", "a b"], "invalid group name"),
    (["--group", "a/b"], "invalid group name"),
    (["--group", "a&b"], "invalid group name"),
    (["--group", "--uninstall"], "invalid group name"),
    (["--user", "--group", "wheel"], "not --user"),
])
def test_group_usage_errors(setup, args, msg):
    src, stage, _, env = setup
    r = install(src, env, *args)
    assert r.returncode == 2
    assert msg in r.stderr
    assert not stage.exists()


def test_install_writes_config_example_but_no_user_config(setup, tmp_path):
    src, stage, _, env = setup
    r = install(src, env)
    assert r.returncode == 0, r.stderr
    example = stage / "usr/local/share/power-watch/config.example"
    assert example.read_text() == (REPO / "config.example").read_text()
    assert not (tmp_path / "home").exists()             # no ~/.config/power-watch/config
    assert not list(stage.rglob("config")) and not (stage / "home").exists()
    assert "cp /usr/local/share/power-watch/config.example ~/.config/power-watch/config" in r.stdout


def test_tmpfiles_failure_is_reported(setup):
    src, _, _, env = setup
    r = install(src, {**env, "SYSTEMD_TMPFILES": "false"})
    assert r.returncode == 1
    assert "sudo systemd-tmpfiles --create /etc/tmpfiles.d/power-watch.conf" in r.stderr


def test_destdir_without_tmpfiles_override_does_not_apply(setup):
    src, _, record, env = setup
    env.pop("SYSTEMD_TMPFILES")
    r = install(src, env)
    assert r.returncode == 0, r.stderr
    assert "not applying" in r.stdout
    assert not record.exists()


def test_live_install_requires_root(setup):
    if os.geteuid() == 0:
        pytest.skip("would install into the real system as root")
    src, _, record, env = setup
    env.pop("DESTDIR")
    r = install(src, env, "--group", "wheel")
    assert r.returncode == 1
    assert "must run as root: sudo ./install.sh --group wheel" in r.stderr
    assert not record.exists()


# ------------------------------------------------------------- --user mode

PLUGIN_ID = "io.github.rsd.power-watch"
SYSTEMCTL_INSTALL = ["--user daemon-reload", "--user enable power-watch.service",
                     "--user restart power-watch.service"]
# one log for omarchy and omarchy-shell ("shell ..."), so their relative order is checked;
# the shell stub notes whether the plugin link existed when it rescanned
RESCAN_LINKED = "shell shell rescanPlugins [linked]"
RESCAN_UNLINKED = "shell shell rescanPlugins [unlinked]"
OMARCHY_ENABLE = [RESCAN_LINKED, "plugin list --json", f"plugin enable {PLUGIN_ID}"]


def recorder(path, log, rc=0):
    path.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit {rc}\n')
    path.chmod(0o755)
    return str(path)


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


def omarchy_stub(path, log, rc=0, discover_after=1):
    """omarchy: logs each call; `plugin list --json` shows the plugin only after a rescan, from
    the `discover_after`-th listing on (the shell discovers it asynchronously); every other
    command exits `rc`."""
    lists = f"{log}.lists"
    path.write_text(f"""#!/bin/sh
echo "$*" >> "{log}"
if [ "$1 $2" = "plugin list" ]; then
    ids='{{"id":"omarchy.clock"}}'
    if [ -e "{log}.rescanned" ]; then
        n=$(($(cat "{lists}" 2>/dev/null || echo 0) + 1))
        echo "$n" > "{lists}"
        [ "$n" -ge {discover_after} ] && ids="$ids"',{{"id": "{PLUGIN_ID}"}}'
    fi
    echo "[$ids]"
    exit 0
fi
exit {rc}
""")
    path.chmod(0o755)
    return str(path)


def shell_stub(path, log, link, rc=0):
    """omarchy-shell: logs "shell <args> [linked|unlinked]"; rc != 0 = shell not running."""
    path.write_text(f"""#!/bin/sh
if [ -L "{link}" ]; then state=linked; else state=unlinked; fi
echo "shell $* [$state]" >> "{log}"
[ {rc} -eq 0 ] || exit {rc}
touch "{log}.rescanned"
""")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def user_setup(setup, tmp_path):
    """--user env: recorders for systemctl and omarchy, plugin dir and HOME in tmp."""
    src, _, _, env = setup
    plugin_src = src / "omarchy-plugin"
    if (REPO / "omarchy-plugin").is_dir():
        shutil.copytree(REPO / "omarchy-plugin", plugin_src)
    else:
        plugin_src.mkdir()
        (plugin_src / "manifest.json").write_text("{}\n")
    env = {k: v for k, v in env.items() if k not in ("DESTDIR", "SYSTEMD_TMPFILES")}
    logs = {"systemctl": tmp_path / "systemctl-calls", "omarchy": tmp_path / "omarchy-calls"}
    link = tmp_path / "plugins" / PLUGIN_ID
    env.update(HOME=str(tmp_path / "home"), OMARCHY_PLUGIN_DIR=str(tmp_path / "plugins"),
               SYSTEMCTL=recorder(tmp_path / "systemctl", logs["systemctl"]),
               OMARCHY=omarchy_stub(tmp_path / "omarchy", logs["omarchy"]),
               OMARCHY_SHELL=shell_stub(tmp_path / "omarchy-shell", logs["omarchy"], link))
    return src, env, logs, link


def test_user_install_links_plugin_enables_and_restarts(user_setup):
    src, env, logs, link = user_setup
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert link.is_symlink() and Path(os.readlink(link)) == src / "omarchy-plugin"
    assert calls(logs["systemctl"]) == SYSTEMCTL_INSTALL
    assert calls(logs["omarchy"]) == OMARCHY_ENABLE  # rescan, discovered, then enable


def test_user_install_waits_for_discovery_before_enabling(user_setup, tmp_path):
    src, env, logs, _ = user_setup
    env["OMARCHY"] = omarchy_stub(tmp_path / "omarchy-slow", logs["omarchy"], discover_after=3)
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(logs["omarchy"]) == [RESCAN_LINKED] + ["plugin list --json"] * 3 + [
        f"plugin enable {PLUGIN_ID}"]


def test_user_install_gives_up_on_discovery_after_2_s(user_setup, tmp_path):
    src, env, logs, _ = user_setup
    env["OMARCHY"] = omarchy_stub(tmp_path / "omarchy-never", logs["omarchy"], discover_after=99)
    started = time.monotonic()
    r = install(src, env, "--user")
    elapsed = time.monotonic() - started
    assert r.returncode == 1
    assert calls(logs["omarchy"]) == [RESCAN_LINKED] + ["plugin list --json"] * 40  # no enable
    assert f"has not discovered {PLUGIN_ID}" in r.stderr
    assert f"retry: omarchy plugin enable {PLUGIN_ID}" in r.stderr
    assert 1.5 < elapsed < 10


def test_user_install_shell_not_running_warns_and_skips_enable(user_setup, tmp_path):
    src, env, logs, link = user_setup
    env["OMARCHY_SHELL"] = shell_stub(tmp_path / "shell-down", logs["omarchy"], link, rc=1)
    r = install(src, env, "--user")
    assert r.returncode == 1
    assert calls(logs["omarchy"]) == [RESCAN_LINKED]  # no polling, no enable
    assert ("retry: omarchy-shell shell rescanPlugins && omarchy plugin enable "
            f"{PLUGIN_ID}") in r.stderr
    assert link.is_symlink() and calls(logs["systemctl"]) == SYSTEMCTL_INSTALL


def test_user_install_replaces_an_existing_symlink(user_setup, tmp_path):
    src, env, _, link = user_setup
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "old-checkout")  # dangling, from a moved checkout
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert Path(os.readlink(link)) == src / "omarchy-plugin"
    assert install(src, env, "--user").returncode == 0  # idempotent upgrade
    assert Path(os.readlink(link)) == src / "omarchy-plugin"


def test_user_install_refuses_to_replace_a_real_dir(user_setup):
    src, env, logs, link = user_setup
    link.mkdir(parents=True)
    (link / "mine").write_text("keep\n")
    r = install(src, env, "--user")
    assert r.returncode == 1
    assert "not a symlink" in r.stderr and "refusing" in r.stderr
    assert not link.is_symlink() and (link / "mine").read_text() == "keep\n"
    assert calls(logs["systemctl"]) == SYSTEMCTL_INSTALL  # the service steps still run
    assert calls(logs["omarchy"]) == []


def test_user_install_default_plugin_dir_is_under_home(user_setup, tmp_path):
    src, env, _, _ = user_setup
    env.pop("OMARCHY_PLUGIN_DIR")
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "home/.config/omarchy/plugins" / PLUGIN_ID).is_symlink()


def test_user_install_systemctl_failures_warn_with_retry_and_exit_1(user_setup, tmp_path):
    src, env, logs, link = user_setup
    env["SYSTEMCTL"] = recorder(tmp_path / "systemctl-fail", logs["systemctl"], rc=1)
    r = install(src, env, "--user")
    assert r.returncode == 1
    assert "retry: systemctl --user daemon-reload" in r.stderr
    assert "retry: systemctl --user enable power-watch.service" in r.stderr
    assert link.is_symlink()  # every other step still ran
    assert calls(logs["omarchy"]) == OMARCHY_ENABLE


def test_user_install_restart_failure_warns(user_setup, tmp_path):
    src, env, logs, _ = user_setup
    script = tmp_path / "systemctl-restart-fails"
    script.write_text(f'#!/bin/sh\necho "$*" >> "{logs["systemctl"]}"\n'
                      '[ "$2" = restart ] && exit 1\nexit 0\n')
    script.chmod(0o755)
    r = install(src, {**env, "SYSTEMCTL": str(script)}, "--user")
    assert r.returncode == 1
    assert calls(logs["systemctl"]) == SYSTEMCTL_INSTALL
    assert "retry: systemctl --user restart power-watch.service" in r.stderr


def test_user_install_omarchy_failure_warns(user_setup, tmp_path):
    src, env, logs, _ = user_setup
    env["OMARCHY"] = omarchy_stub(tmp_path / "omarchy-fail", logs["omarchy"], rc=1)
    r = install(src, env, "--user")
    assert r.returncode == 1
    assert calls(logs["omarchy"]) == OMARCHY_ENABLE
    assert "enabling the Omarchy plugin failed" in r.stderr
    assert f"retry: omarchy plugin enable {PLUGIN_ID}" in r.stderr


def test_user_install_without_omarchy_prints_the_command(user_setup, tmp_path):
    src, env, logs, link = user_setup
    env["OMARCHY"] = str(tmp_path / "no-such-omarchy")
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert f"omarchy plugin enable {PLUGIN_ID}" in r.stdout
    assert link.is_symlink() and calls(logs["systemctl"]) == SYSTEMCTL_INSTALL
    assert calls(logs["omarchy"]) == []  # no rescan either


@pytest.mark.parametrize("args", [["--user", "--uninstall"], ["--uninstall", "--user"]])
def test_user_uninstall_disables_and_removes_link(user_setup, args):
    src, env, logs, link = user_setup
    assert install(src, env, "--user").returncode == 0
    logs["systemctl"].unlink()
    logs["omarchy"].unlink()
    r = install(src, env, *args)
    assert r.returncode == 0, r.stderr
    # disable first, rescan after the link is gone (as omarchy-plugin-remove does)
    assert calls(logs["omarchy"]) == [f"plugin disable {PLUGIN_ID}", RESCAN_UNLINKED]
    assert calls(logs["systemctl"]) == ["--user disable --now power-watch.service"]
    assert not link.is_symlink() and not link.exists()
    assert (src / "omarchy-plugin").is_dir()  # the target is never touched


def test_user_uninstall_leaves_a_real_dir(user_setup):
    src, env, logs, link = user_setup
    link.mkdir(parents=True)
    r = install(src, env, "--user", "--uninstall")
    assert r.returncode == 0, r.stderr
    assert link.is_dir() and "not a symlink" in r.stderr
    assert calls(logs["omarchy"]) == [f"plugin disable {PLUGIN_ID}"]  # nothing removed, no rescan


def test_user_uninstall_with_shell_not_running_still_succeeds(user_setup, tmp_path):
    src, env, logs, link = user_setup
    assert install(src, env, "--user").returncode == 0
    env["OMARCHY_SHELL"] = shell_stub(tmp_path / "shell-down", logs["omarchy"], link, rc=1)
    r = install(src, env, "--user", "--uninstall")
    assert r.returncode == 0, r.stderr
    assert not link.is_symlink() and "next starts" in r.stdout


def test_user_uninstall_failures_warn(user_setup, tmp_path):
    src, env, logs, _ = user_setup
    env["SYSTEMCTL"] = recorder(tmp_path / "systemctl-fail", logs["systemctl"], rc=1)
    env["OMARCHY"] = recorder(tmp_path / "omarchy-fail", logs["omarchy"], rc=1)
    r = install(src, env, "--user", "--uninstall")
    assert r.returncode == 1
    assert "retry: systemctl --user disable --now power-watch.service" in r.stderr
    assert f"retry: omarchy plugin disable {PLUGIN_ID}" in r.stderr


@pytest.mark.parametrize("args", [["--user"], ["--user", "--uninstall"]])
def test_user_mode_refuses_root(user_setup, tmp_path, args):
    src, env, logs, link = user_setup
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "id").write_text("#!/bin/sh\necho 0\n")
    (fake_bin / "id").chmod(0o755)
    r = install(src, {**env, "PATH": f"{fake_bin}:{env['PATH']}"}, *args)
    assert r.returncode == 1
    assert "not root" in r.stderr and " ".join(args) in r.stderr
    assert calls(logs["systemctl"]) == [] and calls(logs["omarchy"]) == []
    assert not link.exists() and not link.is_symlink()


def test_unknown_argument_is_usage_error(setup):
    src, _, _, env = setup
    r = install(src, env, "--bogus")
    assert r.returncode == 2 and "--user" in r.stderr


def test_install_sh_syntax():
    assert subprocess.run(["bash", "-n", str(REPO / "install.sh")], check=False).returncode == 0


def rule_lines():
    text = (REPO / "tmpfiles/power-watch.conf").read_text()
    return [line.split() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def test_tmpfiles_rule_is_z_0440_root_wheel():
    lines = rule_lines()
    assert lines == [["z", "/sys/class/powercap/intel-rapl:*/energy_uj", "0440", "root", "wheel",
                      "-", "-"]]


def test_tmpfiles_glob_covers_package_and_subzones(tmp_path):
    pattern = rule_lines()[0][1]
    base = tmp_path / "sys/class/powercap"
    for zone in ("intel-rapl", "intel-rapl:0", "intel-rapl:0:0", "intel-rapl:0:1"):
        (base / zone).mkdir(parents=True)
        (base / zone / "energy_uj").write_text("1\n")
    (base / "intel-rapl:0" / "nested").mkdir()
    (base / "intel-rapl:0" / "nested" / "energy_uj").write_text("1\n")
    matched = sorted(p.parent.name for p in base.glob(pattern.removeprefix("/sys/class/powercap/")))
    assert matched == ["intel-rapl:0", "intel-rapl:0:0", "intel-rapl:0:1"]
    assert fnmatch.fnmatch("/sys/class/powercap/intel-rapl:0:1/energy_uj", pattern)


def test_user_unit_contents():
    text = (REPO / "systemd/power-watch.service").read_text()
    assert "ExecStart=/usr/local/bin/power-watch --quiet\n" in text
    assert "Restart=on-failure\n" in text and "RestartSec=30\n" in text
    assert "WantedBy=default.target\n" in text
