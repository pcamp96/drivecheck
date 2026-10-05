"""Exercise downloaded installers without packages, services, or host device access."""

import json
import plistlib
import shlex
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from test_installation import ROOT, command, fixture_environment

REVISION = "a" * 40


def environment(tmp_path, platform="Linux"):
    env = fixture_environment(tmp_path)
    commands = tmp_path / "commands"
    real_python = shlex.quote(sys.executable)
    command(commands / "python3", f'''case "$1" in
  */wait-ready.py)
    echo "startup probe $*" >> "{tmp_path}/commands.log"
    if [ "${{FAIL_READINESS:-}}" = 1 ]; then
      echo "DriveCheck startup verification failed. Logs: sudo journalctl -u drivecheck -n 60 --no-pager"
      exit 1
    fi
    echo "Dashboard ready: http://127.0.0.1:8765"
    exit 0
    ;;
esac
if [ "$1" = -m ] && [ "$2" = venv ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/pip"
  chmod +x "$3/bin/pip"
  exit 0
fi
exec {real_python} "$@"''')
    command(commands / "uname", f"echo {platform}")
    command(commands / "sudo", 'exec "$@"')
    prefix = tmp_path / "brew"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/python3.13").symlink_to(commands / "python3")
    command(commands / "brew", f'''echo "brew $*" >> "{tmp_path}/commands.log"
if [ "$1" = --prefix ]; then echo "{prefix}"; fi''')
    command(commands / "launchctl", f'''echo "launchctl $*" >> "{tmp_path}/commands.log"
if [ "$1" = bootout ] && [ "${{FAIL_BOOTOUT:-}}" = 1 ]; then exit 1; fi
exit 0''')
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        files = [ROOT / name for name in (
            "scripts/install-linux.sh", "scripts/uninstall-linux.sh", "scripts/install-macos.sh",
            "scripts/uninstall-macos.sh", "scripts/check-idle.py", "scripts/wait-ready.py", "deploy/drivecheck.service",
            "deploy/run-macos.sh", "pyproject.toml", "requirements.lock", ".env.example",
            "LICENSE", "README.md", "drivecheck/__init__.py",
        )]
        for path in files:
            output.write(path, f"drivecheck-{REVISION}/{path.relative_to(ROOT)}")
    curl = commands / "curl"
    curl.write_text(f'''#!{sys.executable}
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["CURL_LOG"], "a") as output:
    output.write(json.dumps(args) + "\\n")
if os.environ.get("FAIL_DOWNLOAD") == "1":
    sys.exit(22)
if "--output" in args:
    shutil.copyfile(os.environ["ARCHIVE"], args[args.index("--output") + 1])
else:
    print(json.dumps({{"sha": "{REVISION}"}}))
''')
    curl.chmod(0o755)
    env.update(ARCHIVE=str(archive), CURL_LOG=str(tmp_path / "curl.log"))
    return env


def bootstrap(env, *args):
    return subprocess.run(["bash", str(ROOT / "install.sh"), *args], env=env, text=True, capture_output=True)


@pytest.mark.parametrize("platform", ["Linux", "Darwin"])
def test_standalone_install_update_uninstall_preserves_private_state(tmp_path, platform):
    env = environment(tmp_path, platform)
    result = bootstrap(env, "--no-start")
    assert result.returncode == 0, result.stderr
    root = Path(env["DRIVECHECK_TEST_ROOT"])
    config = root / "etc/drivecheck/drivecheck.env"
    data = root / ("var/db/drivecheck" if platform == "Darwin" else "var/lib/drivecheck")
    assert (root / "opt/drivecheck/DEPLOYED_REVISION").read_text().strip() == REVISION
    assert "DRIVECHECK_ALLOW_DESTRUCTIVE=true" in config.read_text()
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    calls = (tmp_path / "commands.log").read_text()
    assert "systemctl start" not in calls and "launchctl bootstrap" not in calls
    if platform == "Darwin":
        plist = plistlib.loads((root / "Library/LaunchDaemons/org.drivecheck.station.plist").read_bytes())
        assert plist["ProgramArguments"] == [str(root / "opt/drivecheck/run-macos.sh")]
        assert plist["Umask"] == 0o77
        assert "brew install python@3.13 fio smartmontools" in calls
        assert "DRIVECHECK_DATA_DIR=/var/db/drivecheck" in config.read_text()
    config.write_text("DRIVECHECK_ALLOW_DESTRUCTIVE=false\n")
    (data / "report").write_text("preserved")
    result = bootstrap(env, "--ref", REVISION.upper())
    assert result.returncode == 0, result.stderr
    assert config.read_text() == "DRIVECHECK_ALLOW_DESTRUCTIVE=false\n"
    assert (data / "report").read_text() == "preserved"
    assert not list(tmp_path.glob("drivecheck-install.*"))
    subprocess.run(["bash", str(root / "usr/local/sbin/drivecheck-uninstall")], env=env, check=True)
    assert not (root / "opt/drivecheck").exists()
    assert config.exists() and (data / "report").exists()
    result = bootstrap(env, "--no-start")
    assert result.returncode == 0, result.stderr
    subprocess.run(["bash", str(root / "usr/local/sbin/drivecheck-uninstall"), "--purge-data"], env=env, check=True)
    assert not data.exists() and not config.exists()


@pytest.mark.parametrize("platform", ["Linux", "Darwin"])
def test_dry_run_only_resolves_revision_and_download_failure_cleans_up(tmp_path, platform):
    env = environment(tmp_path, platform)
    result = bootstrap(env, "--dry-run", "--ref", "release/test")
    assert result.returncode == 0, result.stderr
    assert ("launchd" if platform == "Darwin" else "systemd") in result.stdout
    assert "release%2Ftest" in (tmp_path / "curl.log").read_text()
    assert "--output" not in (tmp_path / "curl.log").read_text()
    assert not Path(env["DRIVECHECK_TEST_ROOT"]).exists()
    env["FAIL_DOWNLOAD"] = "1"
    result = bootstrap(env, "--ref", REVISION)
    assert result.returncode != 0
    assert not list(tmp_path.glob("drivecheck-install.*"))
    assert not Path(env["DRIVECHECK_TEST_ROOT"]).exists()


@pytest.mark.parametrize("bad_entry", ["../escaped", "drivecheck-" + REVISION + "/../../escaped", "symlink", "incomplete"])
def test_rejects_unsafe_or_incomplete_archives_before_native_install(tmp_path, bad_entry):
    env = environment(tmp_path)
    with zipfile.ZipFile(env["ARCHIVE"], "w") as output:
        if bad_entry == "symlink":
            entry = zipfile.ZipInfo(f"drivecheck-{REVISION}/evil")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(entry, "/etc")
        else:
            output.writestr(bad_entry, "no")
    result = bootstrap(env, "--ref", REVISION)
    assert result.returncode != 0
    assert not Path(env["DRIVECHECK_TEST_ROOT"]).exists()
    assert not (tmp_path / "commands.log").exists()
    assert not (tmp_path / "escaped").exists()
    assert not list(tmp_path.glob("drivecheck-install.*"))


@pytest.mark.parametrize("platform", ["Linux", "Darwin"])
@pytest.mark.parametrize("run", [
    {"status": "running"}, {"status": "queued"},
    {"status": "passed", "workflow_status": "finishing"},
    {"status": "passed", "workflow_status": "awaiting_action"},
])
def test_update_refuses_unfinished_work_before_packages_or_stop(tmp_path, platform, run):
    env = environment(tmp_path, platform)
    assert bootstrap(env, "--no-start").returncode == 0
    root = Path(env["DRIVECHECK_TEST_ROOT"])
    data = root / ("var/db/drivecheck" if platform == "Darwin" else "var/lib/drivecheck")
    with sqlite3.connect(data / "drivecheck.sqlite3") as database:
        database.execute("CREATE TABLE runs (data TEXT)")
        database.execute("INSERT INTO runs VALUES (?)", (json.dumps(run),))
    (tmp_path / "commands.log").unlink()
    result = bootstrap(env)
    assert result.returncode != 0
    assert "unfinished work" in result.stderr
    assert not (tmp_path / "commands.log").exists()
    assert (root / "opt/drivecheck/DEPLOYED_REVISION").exists()


def test_macos_stop_failure_preserves_installed_app(tmp_path):
    env = environment(tmp_path, "Darwin")
    assert bootstrap(env, "--no-start").returncode == 0
    env["FAIL_BOOTOUT"] = "1"
    result = bootstrap(env)
    assert result.returncode != 0
    assert (Path(env["DRIVECHECK_TEST_ROOT"]) / "opt/drivecheck/README.md").exists()
    result = subprocess.run(["bash", str(ROOT / "scripts/uninstall-macos.sh")], env=env)
    assert result.returncode != 0
    assert (Path(env["DRIVECHECK_TEST_ROOT"]) / "opt/drivecheck/README.md").exists()


@pytest.mark.parametrize("platform", ["Linux", "Darwin"])
def test_completed_released_job_does_not_block_update(tmp_path, platform):
    env = environment(tmp_path, platform)
    assert bootstrap(env, "--no-start").returncode == 0
    root = Path(env["DRIVECHECK_TEST_ROOT"])
    data = root / ("var/db/drivecheck" if platform == "Darwin" else "var/lib/drivecheck")
    with sqlite3.connect(data / "drivecheck.sqlite3") as database:
        database.execute("CREATE TABLE runs (data TEXT)")
        database.execute("INSERT INTO runs VALUES (?)", (json.dumps({"status": "passed", "workflow_status": "released"}),))
    result = bootstrap(env, "--no-start")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("bad_ref", ["--bad", "bad?ref", "a" * 201, ""])
def test_invalid_revision_never_downloads(tmp_path, bad_ref):
    env = environment(tmp_path)
    assert bootstrap(env, "--ref", bad_ref).returncode == 2
    assert not (tmp_path / "curl.log").exists()


def test_macos_unmanaged_paths_and_marker_symlink_are_preserved(tmp_path):
    env = environment(tmp_path, "Darwin")
    root = Path(env["DRIVECHECK_TEST_ROOT"])
    app = root / "opt/drivecheck"
    app.mkdir(parents=True)
    sentinel = app / "unrelated"
    sentinel.write_text("keep")
    result = bootstrap(env)
    assert result.returncode != 0
    assert "ownership marker" in result.stderr
    assert sentinel.read_text() == "keep"
    sentinel.unlink()
    assert bootstrap(env, "--no-start").returncode == 0
    marker = root / "etc/drivecheck/.managed-by-drivecheck"
    marker.unlink()
    target = tmp_path / "marker"
    target.write_text("drivecheck-macos-install-v1")
    marker.symlink_to(target)
    for script in [ROOT / "scripts/install-macos.sh", ROOT / "scripts/uninstall-macos.sh"]:
        result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
        assert result.returncode != 0
        assert (app / "README.md").exists()


@pytest.mark.parametrize("platform", ["Linux", "Darwin"])
def test_installer_only_reports_success_after_readiness_probe(tmp_path, platform):
    env = environment(tmp_path, platform)
    env["FAIL_READINESS"] = "1"
    result = bootstrap(env)
    assert result.returncode != 0
    assert "startup verification failed" in result.stderr
    assert "DriveCheck installed on" not in result.stdout
    assert "Installed revision:" not in result.stdout
    root = Path(env["DRIVECHECK_TEST_ROOT"])
    assert (root / "etc/drivecheck/drivecheck.env").is_file()
    assert (root / "usr/local/sbin/drivecheck-uninstall").is_file()
    env.pop("FAIL_READINESS")
    result = bootstrap(env)
    assert result.returncode == 0, result.stderr
    assert "Dashboard ready" in result.stdout


@pytest.mark.parametrize("platform", ["Linux", "Darwin"])
def test_no_start_does_not_offer_a_token_before_startup(tmp_path, platform):
    env = environment(tmp_path, platform)
    result = bootstrap(env, "--no-start")
    assert result.returncode == 0, result.stderr
    assert "generated on first startup" in result.stdout
    assert "Sign-in token: sudo cat" not in result.stdout
    assert "startup probe" not in (tmp_path / "commands.log").read_text()
