"""Tests for install.sh into a DESTDIR staging root, plus the tmpfiles rule and user unit."""

import fnmatch
import os
import pty
import shutil
import stat
import subprocess
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
PLUGIN_URL = "https://github.com/rsd/omarchy-power-watch"
PLUGIN_ADD = f"omarchy plugin add {PLUGIN_URL} --enable"
PLUGIN_DISABLE = f"omarchy plugin disable {PLUGIN_ID}"
PLUGIN_LIST = "omarchy plugin list --json"
RESCAN = "omarchy-shell shell rescanPlugins"
# systemctl, omarchy and omarchy-shell log to one file ("<name> <args>"), so their relative order is checked
SYSTEMCTL_INSTALL = ["systemctl --user daemon-reload",
                     "systemctl --user enable power-watch.service",
                     "systemctl --user restart power-watch.service"]
SYSTEMCTL_UNINSTALL = ["systemctl --user disable --now power-watch.service"]


def recorder(path, log, name, rc=0):
    path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit {rc}\n')
    path.chmod(0o755)
    return str(path)


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def user_setup(setup, tmp_path):
    """--user env: recorders for systemctl, omarchy and omarchy-shell, plugin dir and HOME in
    tmp, and an interactive session (POWER_WATCH_INTERACTIVE=1) unless a test says otherwise."""
    src, _, _, env = setup
    env = {k: v for k, v in env.items() if k not in ("DESTDIR", "SYSTEMD_TMPFILES")}
    log = tmp_path / "calls"
    plugin = tmp_path / "plugins" / PLUGIN_ID
    env.update(HOME=str(tmp_path / "home"), OMARCHY_PLUGIN_DIR=str(tmp_path / "plugins"),
               SYSTEMCTL=recorder(tmp_path / "systemctl", log, "systemctl"),
               OMARCHY=recorder(tmp_path / "omarchy", log, "omarchy"),
               OMARCHY_SHELL=recorder(tmp_path / "omarchy-shell", log, "omarchy-shell"),
               POWER_WATCH_INTERACTIVE="1")
    return src, env, log, plugin


def lister(path, log, listing):
    """omarchy stub: records every call like recorder; `plugin list --json` prints `listing`."""
    out = path.with_name(path.name + ".json")
    out.write_text(listing)
    path.write_text(f'#!/bin/sh\necho "omarchy $*" >> "{log}"\n'
                    f'[ "$1 $2 $3" = "plugin list --json" ] && cat "{out}"\nexit 0\n')
    path.chmod(0o755)
    return str(path)


def legacy_link(src, plugin, target_exists, checkout=None):
    """The link the old installer made: <plugin dir>/<id> -> <checkout>/omarchy-plugin (the
    checkout as `src`, or as another path to it)."""
    target = (checkout or src) / "omarchy-plugin"
    if target_exists:
        target.mkdir()
        (target / "manifest.json").write_text("{}\n")
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.symlink_to(target)
    return target


def test_user_install_enables_service_then_offers_plugin(user_setup):
    src, env, log, plugin = user_setup
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL + [PLUGIN_LIST, PLUGIN_ADD]  # no --yes: omarchy asks
    assert not plugin.exists() and not plugin.is_symlink()  # install.sh links nothing itself
    assert not plugin.parent.exists()


def test_user_install_non_interactive_prints_the_add_command(user_setup):
    src, env, log, _ = user_setup
    r = install(src, {**env, "POWER_WATCH_INTERACTIVE": "0"}, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL + [PLUGIN_LIST]
    assert f"to add it run: {PLUGIN_ADD}" in r.stdout


def test_user_install_detects_no_terminal(user_setup):
    src, env, log, _ = user_setup
    env.pop("POWER_WATCH_INTERACTIVE")
    r = install(src, env, "--user")  # captured stdout is not a terminal
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL + [PLUGIN_LIST]
    assert PLUGIN_ADD in r.stdout


def test_user_install_detects_a_terminal(user_setup):
    src, env, log, _ = user_setup
    env.pop("POWER_WATCH_INTERACTIVE")
    leader, follower = pty.openpty()
    try:
        r = subprocess.run(["bash", str(src / "install.sh"), "--user"], stdin=follower,
                           stdout=follower, stderr=subprocess.PIPE, text=True, env=env,
                           check=False, timeout=30)
    finally:
        os.close(follower)
        os.close(leader)
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL + [PLUGIN_LIST, PLUGIN_ADD]


def test_user_install_without_omarchy_prints_the_add_command(user_setup, tmp_path):
    src, env, log, _ = user_setup
    r = install(src, {**env, "OMARCHY": str(tmp_path / "no-such-omarchy")}, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL
    assert f"omarchy not found; for the Omarchy bar widget run: {PLUGIN_ADD}" in r.stdout


@pytest.mark.parametrize("kind", ["dir", "symlink"])
def test_user_install_leaves_an_installed_plugin_alone(user_setup, tmp_path, kind):
    src, env, log, plugin = user_setup
    plugin.parent.mkdir(parents=True)
    if kind == "dir":  # what `omarchy plugin add` makes
        plugin.mkdir()
        (plugin / "mine").write_text("keep\n")
    else:  # a development checkout, linked by hand
        (tmp_path / "checkout").mkdir()
        plugin.symlink_to(tmp_path / "checkout")
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL  # no add, no disable
    assert "already installed" in r.stdout
    if kind == "dir":
        assert not plugin.is_symlink() and (plugin / "mine").read_text() == "keep\n"
    else:
        assert Path(os.readlink(plugin)) == tmp_path / "checkout"


def test_user_install_dangling_symlink_elsewhere_warns(user_setup, tmp_path):
    src, env, log, plugin = user_setup
    plugin.parent.mkdir(parents=True)
    plugin.symlink_to(tmp_path / "moved-away")
    r = install(src, env, "--user")
    assert r.returncode == 1
    assert calls(log) == SYSTEMCTL_INSTALL + [PLUGIN_LIST]  # add would refuse the taken path
    assert f"retry: rm {plugin} && {PLUGIN_ADD}" in r.stderr
    assert plugin.is_symlink()  # not ours to remove


def test_user_install_default_plugin_dir_is_under_home(user_setup, tmp_path):
    src, env, log, _ = user_setup
    env.pop("OMARCHY_PLUGIN_DIR")
    (tmp_path / "home/.config/omarchy/plugins" / PLUGIN_ID).mkdir(parents=True)
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL and "already installed" in r.stdout


def test_user_install_plugin_add_declined_or_failed_is_not_an_error(user_setup, tmp_path):
    """omarchy plugin add exits 1 also when you decline its prompt ("aborted"): the add command
    is printed for later, and the run still succeeds."""
    src, env, log, _ = user_setup
    env["OMARCHY"] = recorder(tmp_path / "omarchy-fail", log, "omarchy", rc=1)
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL + [PLUGIN_LIST, PLUGIN_ADD]
    assert f"Omarchy bar widget not added; to add it later run: {PLUGIN_ADD}" in r.stdout
    assert "retry" not in r.stderr


@pytest.mark.parametrize("listing", [
    f'[{{"id":"{PLUGIN_ID}","enabled":true}}]',
    f'[\n  {{\n    "name": "Power",\n    "id" :\t "{PLUGIN_ID}"\n  }}\n]\n',
])
def test_user_install_plugin_listed_elsewhere_is_installed(user_setup, tmp_path, listing):
    """omarchy refuses an id it finds in another plugin dir: no offer when it lists the id."""
    src, env, log, plugin = user_setup
    env["OMARCHY"] = lister(tmp_path / "omarchy-lists", log, listing)
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL + [PLUGIN_LIST]  # no add
    assert f"already installed (omarchy plugin list reports {PLUGIN_ID})" in r.stdout
    assert not plugin.exists() and not plugin.is_symlink()


@pytest.mark.parametrize("listing", ["[]\n", f'[{{"id":"{PLUGIN_ID}-extra"}}]',
                                     f'[{{"name":"{PLUGIN_ID}"}}]', "not json"])
def test_user_install_plugin_not_listed_is_offered(user_setup, tmp_path, listing):
    src, env, log, _ = user_setup
    env["OMARCHY"] = lister(tmp_path / "omarchy-lists", log, listing)
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL + [PLUGIN_LIST, PLUGIN_ADD]


@pytest.mark.parametrize("target_exists", [True, False])
@pytest.mark.parametrize("interactive", ["1", "0"])
def test_user_install_replaces_the_legacy_link(user_setup, target_exists, interactive):
    src, env, log, plugin = user_setup
    target = legacy_link(src, plugin, target_exists)
    r = install(src, {**env, "POWER_WATCH_INTERACTIVE": interactive}, "--user")
    assert r.returncode == 0, r.stderr
    offer = [PLUGIN_ADD] if interactive == "1" else []
    assert calls(log) == [PLUGIN_DISABLE, RESCAN] + SYSTEMCTL_INSTALL + [PLUGIN_LIST] + offer
    assert not plugin.exists() and not plugin.is_symlink()
    assert f"Removed the old bundled plugin link {plugin}" in r.stdout
    assert target.is_dir() == target_exists  # the link target is never touched
    if interactive == "0":
        assert PLUGIN_ADD in r.stdout


def test_user_install_legacy_link_without_omarchy_is_still_removed(user_setup, tmp_path):
    src, env, log, plugin = user_setup
    legacy_link(src, plugin, target_exists=False)
    r = install(src, {**env, "OMARCHY": str(tmp_path / "no-such-omarchy")}, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == [RESCAN] + SYSTEMCTL_INSTALL
    assert not plugin.is_symlink() and PLUGIN_ADD in r.stdout


def test_user_install_legacy_disable_failure_is_info_and_still_removes(user_setup, tmp_path):
    """The usual upgrade: the link target is gone (or no shell runs), so omarchy cannot disable
    the plugin. That is not an error and no retry is offered; the link still goes."""
    src, env, log, plugin = user_setup
    legacy_link(src, plugin, target_exists=False)
    env["OMARCHY"] = recorder(tmp_path / "omarchy-fail", log, "omarchy", rc=1)
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert "Could not disable the old bundled plugin" in r.stdout
    assert "retry" not in r.stderr
    assert not plugin.is_symlink()
    assert calls(log) == [PLUGIN_DISABLE, RESCAN] + SYSTEMCTL_INSTALL + [PLUGIN_LIST, PLUGIN_ADD]


@pytest.mark.parametrize("link_via", ["physical", "alias"])
def test_user_install_legacy_link_by_another_path_to_the_checkout(user_setup, tmp_path,
                                                                  link_via):
    """install.sh run through a symlinked path to the checkout still knows its old link, made
    through either path (target still present, so only the path match can recognise it)."""
    src, env, log, plugin = user_setup
    alias = tmp_path / "alias"
    alias.symlink_to(src)
    physical = Path(os.path.realpath(src))
    target = legacy_link(src, plugin, target_exists=True,
                         checkout=physical if link_via == "physical" else alias)
    r = subprocess.run(["bash", str(alias / "install.sh"), "--user"], capture_output=True,
                       text=True, env=env, check=False)
    assert r.returncode == 0, r.stderr
    assert calls(log)[:2] == [PLUGIN_DISABLE, RESCAN]
    assert not plugin.exists() and not plugin.is_symlink()
    assert target.is_dir()


def test_user_install_dangling_legacy_link_from_another_checkout(user_setup, tmp_path):
    src, env, log, plugin = user_setup
    plugin.parent.mkdir(parents=True)
    plugin.symlink_to(tmp_path / "old-clone" / "power-watch" / "omarchy-plugin")
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == [PLUGIN_DISABLE, RESCAN] + SYSTEMCTL_INSTALL + [PLUGIN_LIST, PLUGIN_ADD]
    assert not plugin.is_symlink()
    assert "Removed the old bundled plugin link" in r.stdout


def test_user_install_live_link_to_another_omarchy_plugin_dir_is_not_legacy(user_setup,
                                                                            tmp_path):
    src, env, log, plugin = user_setup
    other = tmp_path / "other-checkout" / "omarchy-plugin"
    other.mkdir(parents=True)
    plugin.parent.mkdir(parents=True)
    plugin.symlink_to(other)
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL  # not disabled, not removed, no offer
    assert Path(os.readlink(plugin)) == other and "already installed" in r.stdout


@pytest.mark.parametrize("shell", ["fails", "absent"])
def test_user_install_rescan_failure_is_not_an_error(user_setup, tmp_path, shell):
    src, env, log, plugin = user_setup
    legacy_link(src, plugin, target_exists=False)
    if shell == "fails":  # the shell is not running
        env["OMARCHY_SHELL"] = recorder(tmp_path / "shell-fail", log, "omarchy-shell", rc=1)
    else:
        env["OMARCHY_SHELL"] = str(tmp_path / "no-such-omarchy-shell")
    r = install(src, env, "--user")
    assert r.returncode == 0, r.stderr
    assert not plugin.is_symlink()
    rescans = [RESCAN] if shell == "fails" else []
    assert calls(log) == [PLUGIN_DISABLE] + rescans + SYSTEMCTL_INSTALL + [PLUGIN_LIST, PLUGIN_ADD]
    unreachable = "Omarchy shell not reachable" in r.stdout
    assert unreachable == (shell == "fails")
    assert "retry" not in r.stderr


def test_user_install_systemctl_failures_warn_with_retry_and_exit_1(user_setup, tmp_path):
    src, env, log, _ = user_setup
    env["SYSTEMCTL"] = recorder(tmp_path / "systemctl-fail", log, "systemctl", rc=1)
    r = install(src, env, "--user")
    assert r.returncode == 1
    assert "retry: systemctl --user daemon-reload" in r.stderr
    assert "retry: systemctl --user enable power-watch.service" in r.stderr
    assert calls(log) == SYSTEMCTL_INSTALL[:2] + [PLUGIN_LIST, PLUGIN_ADD]  # the offer still runs


def test_user_install_restart_failure_warns(user_setup, tmp_path):
    src, env, log, _ = user_setup
    script = tmp_path / "systemctl-restart-fails"
    script.write_text(f'#!/bin/sh\necho "systemctl $*" >> "{log}"\n'
                      '[ "$2" = restart ] && exit 1\nexit 0\n')
    script.chmod(0o755)
    r = install(src, {**env, "SYSTEMCTL": str(script)}, "--user")
    assert r.returncode == 1
    assert calls(log) == SYSTEMCTL_INSTALL + [PLUGIN_LIST, PLUGIN_ADD]
    assert "retry: systemctl --user restart power-watch.service" in r.stderr


@pytest.mark.parametrize("args", [["--user", "--uninstall"], ["--uninstall", "--user"]])
def test_user_uninstall_disables_the_service(user_setup, args):
    src, env, log, plugin = user_setup
    r = install(src, env, *args)
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_UNINSTALL
    assert "omarchy plugin remove" not in r.stdout
    assert not plugin.exists() and not plugin.is_symlink()


@pytest.mark.parametrize("kind", ["dir", "symlink"])
def test_user_uninstall_leaves_an_installed_plugin_with_a_hint(user_setup, tmp_path, kind):
    src, env, log, plugin = user_setup
    plugin.parent.mkdir(parents=True)
    if kind == "dir":
        plugin.mkdir()
        (plugin / "mine").write_text("keep\n")
    else:
        (tmp_path / "checkout").mkdir()
        plugin.symlink_to(tmp_path / "checkout")
    r = install(src, env, "--user", "--uninstall")
    assert r.returncode == 0, r.stderr
    assert calls(log) == SYSTEMCTL_UNINSTALL  # omarchy is not touched
    assert f"omarchy plugin remove {PLUGIN_ID}" in r.stdout
    if kind == "dir":
        assert (plugin / "mine").read_text() == "keep\n"
    else:
        assert Path(os.readlink(plugin)) == tmp_path / "checkout"


@pytest.mark.parametrize("target_exists", [True, False])
def test_user_uninstall_removes_the_legacy_link(user_setup, target_exists):
    src, env, log, plugin = user_setup
    target = legacy_link(src, plugin, target_exists)
    r = install(src, env, "--user", "--uninstall")
    assert r.returncode == 0, r.stderr
    assert calls(log) == [PLUGIN_DISABLE, RESCAN] + SYSTEMCTL_UNINSTALL
    assert not plugin.exists() and not plugin.is_symlink()
    assert target.is_dir() == target_exists
    assert "omarchy plugin remove" not in r.stdout


def test_user_uninstall_failures_warn(user_setup, tmp_path):
    src, env, log, plugin = user_setup
    legacy_link(src, plugin, target_exists=False)
    env["SYSTEMCTL"] = recorder(tmp_path / "systemctl-fail", log, "systemctl", rc=1)
    env["OMARCHY"] = recorder(tmp_path / "omarchy-fail", log, "omarchy", rc=1)
    r = install(src, env, "--user", "--uninstall")
    assert r.returncode == 1
    assert "retry: systemctl --user disable --now power-watch.service" in r.stderr
    assert PLUGIN_DISABLE not in r.stderr  # a failed disable of the old link is information
    assert "Could not disable the old bundled plugin" in r.stdout
    assert not plugin.is_symlink()


@pytest.mark.parametrize("args", [["--user"], ["--user", "--uninstall"]])
def test_user_mode_refuses_root(user_setup, tmp_path, args):
    src, env, log, plugin = user_setup
    legacy_link(src, plugin, target_exists=False)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "id").write_text("#!/bin/sh\necho 0\n")
    (fake_bin / "id").chmod(0o755)
    r = install(src, {**env, "PATH": f"{fake_bin}:{env['PATH']}"}, *args)
    assert r.returncode == 1
    assert "not root" in r.stderr and " ".join(args) in r.stderr
    assert calls(log) == []
    assert plugin.is_symlink()  # nothing touched, not even the legacy link


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
