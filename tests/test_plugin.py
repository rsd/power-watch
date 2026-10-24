"""Tests for the Omarchy shell plugin in omarchy-plugin/: the node model suite and the manifest."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "omarchy-plugin"
MODEL_TEST = REPO / "tests" / "plugin" / "model-test.js"
QMLLINT = Path("/usr/lib/qt6/bin/qmllint")
SHELL_DIR = Path(os.environ.get("OMARCHY_SHELL_DIR", "/usr/share/omarchy/shell"))


@pytest.fixture(scope="module")
def manifest():
    return json.loads((PLUGIN / "manifest.json").read_text())


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_model_suite_passes():
    result = subprocess.run(["node", str(MODEL_TEST)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^(\d+)/\1 passed$", result.stdout, re.M), result.stdout


def test_manifest_identity(manifest):
    assert manifest["schemaVersion"] == 1
    assert manifest["id"] == "io.github.rsd.power-watch"
    assert manifest["name"] == "Power Watch"
    assert manifest["author"] == "Raul Dias"
    assert manifest["license"] == "MIT"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["description"]


def test_manifest_bar_widget(manifest):
    assert manifest["kinds"] == ["bar-widget"]
    widget = manifest["barWidget"]
    assert widget["defaultSection"] == "right"
    assert widget["allowMultiple"] is False
    assert widget["displayName"] == "Power Watch"


def test_entry_points_exist_and_are_safe(manifest):
    entry = manifest["entryPoints"]
    assert set(entry) == {"barWidget"}
    for rel in entry.values():
        assert not rel.startswith("/") and ".." not in rel
        assert (PLUGIN / rel).is_file(), rel


def test_qml_imports_resolve_inside_the_plugin():
    # A JS or QML file imported by relative path must ship with the plugin, and
    # so must the sibling components BarWidget.qml uses by type name.
    for qml in PLUGIN.glob("*.qml"):
        for rel in re.findall(r'^import\s+"([^"]+)"', qml.read_text(), re.M):
            assert (PLUGIN / rel).is_file(), f"{qml.name} imports missing {rel}"
    bar = (PLUGIN / "BarWidget.qml").read_text()
    for component in ("Chart", "GatedFile"):
        assert re.search(rf"^\s*{component}\s*{{", bar, re.M), component
        assert (PLUGIN / f"{component}.qml").is_file(), component


@pytest.mark.skipif(not QMLLINT.is_file() or not (SHELL_DIR / "Ui").is_dir(),
                    reason="Qt 6 qmllint or the Omarchy shell sources are not installed")
def test_qmllint_every_qml_file(tmp_path):
    # Qt 6's linter (/usr/bin/qmllint is Qt 5's). It resolves `qs.Ui` only from
    # a <root>/qs/Ui layout, hence a throwaway shim of symlinks outside the
    # plugin (a symlink inside a plugin folder makes the shell refuse it).
    #
    # Fatal: a non-zero exit (syntax) and any [import] or [unresolved-type]
    # warning, which qmllint exits 0 on while the engine refuses to load the
    # file — in the shell, a widget that is silently not there. BarWidget.qml's
    # remaining warnings are inherent (`bar` typed QtObject, Style singletons
    # typed QObject, inline components reaching `root`) and come out of the
    # first-party widgets too. The other files lint clean, and must stay so:
    # this qmllint does not always say [import] for a missing module (measured:
    # GatedFile.qml without its folderlistmodel import gets one [unqualified]
    # and nothing else), so any warning there is fatal.
    shim = tmp_path / "qs"
    shim.mkdir()
    (shim / "Ui").symlink_to(SHELL_DIR / "Ui")
    (shim / "Commons").symlink_to(SHELL_DIR / "Commons")
    files = sorted(str(p) for p in PLUGIN.glob("*.qml"))
    assert len(files) >= 3, files
    result = subprocess.run([str(QMLLINT), "-I", str(tmp_path), "-I", "/usr/lib/qt6/qml", *files],
                            capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr
    fatal = [line for line in output.splitlines()
             if line.startswith("Warning:") and (re.search(r"\[(syntax|import|unresolved-type)\]", line)
                                                  or "/BarWidget.qml:" not in line)]
    assert result.returncode == 0 and not fatal, "\n".join(fatal[:10]) or output[-2000:]


def test_no_symlinks_inside_plugin():
    # omarchy plugin validate refuses any symlink inside a plugin folder.
    assert [p for p in PLUGIN.rglob("*") if p.is_symlink()] == []


@pytest.mark.skipif(shutil.which("omarchy-plugin-validate") is None, reason="omarchy is not installed")
def test_omarchy_validate_accepts_plugin():
    result = subprocess.run(["omarchy-plugin-validate", str(PLUGIN)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
