import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def command(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def fixture_environment(tmp_path: Path) -> dict[str, str]:
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "commands.log"
    packages_installed = tmp_path / "packages-installed"
    command(
        commands / "dpkg-query",
        f'if [ -f "{packages_installed}" ]; then echo "install ok installed"; exit 0; fi; exit 1',
    )
    command(
        commands / "apt-get",
        f'echo "apt-get $*" >> "{log}"; [ "$1" = install ] && touch "{packages_installed}"; exit 0',
    )
    command(
        commands / "systemctl",
        f'''echo "systemctl $*" >> "{log}"
if [ "$1" = list-unit-files ] && [ "$2" = drivecheck.service ]; then
  echo "drivecheck.service enabled"
fi
if [ "$1" = list-unit-files ] && [ -f "{packages_installed}" ]; then
  case "$2" in smartmontools.service|smartd.service) echo "$2 enabled" ;; esac
fi
if [ "$1" = show ]; then
  echo "smartmontools.service"
fi
if [ "$1" = stop ] && [ "${{FAIL_DRIVECHECK_STOP:-}}" = 1 ]; then
  exit 1
fi
exit 0''',
    )
    command(commands / "mountpoint", "exit 1")
    command(
        commands / "python3",
        """if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/pip"
  chmod +x "$3/bin/pip"
fi
exit 0""",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{commands}:{env['PATH']}",
            "DRIVECHECK_TEST_ROOT": str(tmp_path / "root"),
            "DRIVECHECK_INSTALL_TESTING": "1",
        }
    )
    return env


def test_reinstall_and_default_uninstall_preserve_config_and_data(tmp_path):
    env = fixture_environment(tmp_path)
    root = Path(env["DRIVECHECK_TEST_ROOT"])
    config = root / "etc/drivecheck/drivecheck.env"
    data = root / "var/lib/drivecheck/report.json"

    subprocess.run(
        ["bash", str(ROOT / "scripts/install-linux.sh"), "--no-start"], env=env, check=True
    )
    initial = config.read_text()
    assert "DRIVECHECK_HEADLESS=false" in initial
    assert "DRIVECHECK_ALLOW_DESTRUCTIVE=true" in initial
    assert (root / "opt/drivecheck/LICENSE").is_file()
    assert (root / "opt/drivecheck/README.md").is_file()
    manifest = root / "etc/drivecheck/install-manifest"
    initially_missing = next(
        line for line in manifest.read_text().splitlines() if line.startswith("apt_packages_")
    )
    command_log = (tmp_path / "commands.log").read_text().splitlines()
    smart_disables = [line for line in command_log if "disable --now smart" in line]
    assert smart_disables == ["systemctl disable --now smartmontools.service"]

    config.write_text("DRIVECHECK_HEADLESS=true\n")
    data.write_text("keep me\n")
    subprocess.run(
        ["bash", str(ROOT / "scripts/install-linux.sh"), "--no-start"], env=env, check=True
    )
    assert config.read_text() == "DRIVECHECK_HEADLESS=true\n"
    assert data.read_text() == "keep me\n"
    assert (root / "usr/local/sbin/drivecheck-uninstall").is_file()
    assert initially_missing
    assert initially_missing in manifest.read_text().splitlines()

    subprocess.run(["bash", str(ROOT / "scripts/uninstall-linux.sh")], env=env, check=True)
    assert not (root / "opt/drivecheck").exists()
    assert not (root / "etc/systemd/system/drivecheck.service").exists()
    assert not (root / "usr/local/sbin/drivecheck-uninstall").exists()
    assert config.read_text() == "DRIVECHECK_HEADLESS=true\n"
    assert data.read_text() == "keep me\n"


def test_purge_removes_only_guarded_config_and_data(tmp_path):
    env = fixture_environment(tmp_path)
    root = Path(env["DRIVECHECK_TEST_ROOT"])
    subprocess.run(
        ["bash", str(ROOT / "scripts/install-linux.sh"), "--no-start"], env=env, check=True
    )
    unrelated = root / "var/lib/unrelated"
    unrelated.mkdir(parents=True)
    (unrelated / "keep").write_text("safe")

    subprocess.run(
        ["bash", str(ROOT / "scripts/uninstall-linux.sh"), "--purge-data"], env=env, check=True
    )
    assert not (root / "etc/drivecheck").exists()
    assert not (root / "var/lib/drivecheck").exists()
    assert (unrelated / "keep").read_text() == "safe"


def test_installer_refuses_an_unmarked_existing_application(tmp_path):
    env = fixture_environment(tmp_path)
    app = Path(env["DRIVECHECK_TEST_ROOT"]) / "opt/drivecheck"
    app.mkdir(parents=True)
    sentinel = app / "not-drivecheck"
    sentinel.write_text("do not replace")

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install-linux.sh"), "--no-start"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "ownership marker" in result.stderr
    assert sentinel.read_text() == "do not replace"


def test_uninstall_rejects_marker_symlink_and_stop_failure(tmp_path):
    env = fixture_environment(tmp_path)
    root = Path(env["DRIVECHECK_TEST_ROOT"])
    subprocess.run(
        ["bash", str(ROOT / "scripts/install-linux.sh"), "--no-start"], env=env, check=True
    )
    marker = root / "etc/drivecheck/.managed-by-drivecheck"
    marker.unlink()
    marker.symlink_to(root / "elsewhere")
    (root / "elsewhere").write_text("drivecheck-linux-install-v1\n")
    rejected = subprocess.run(
        ["bash", str(ROOT / "scripts/uninstall-linux.sh")], env=env, capture_output=True, text=True
    )
    assert rejected.returncode != 0
    assert (root / "opt/drivecheck").exists()

    marker.unlink()
    marker.write_text("drivecheck-linux-install-v1\n")
    env["FAIL_DRIVECHECK_STOP"] = "1"
    rejected = subprocess.run(
        ["bash", str(ROOT / "scripts/uninstall-linux.sh")], env=env, capture_output=True, text=True
    )
    assert rejected.returncode != 0
    assert (root / "opt/drivecheck").exists()


def test_failed_build_is_owned_and_test_root_must_be_safe(tmp_path):
    env = fixture_environment(tmp_path)
    root = Path(env["DRIVECHECK_TEST_ROOT"])
    python = Path(env["PATH"].split(":", 1)[0]) / "python3"
    command(
        python,
        """if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\nexit 42\\n' > "$3/bin/pip"
  chmod +x "$3/bin/pip"
fi
exit 0""",
    )
    failed = subprocess.run(["bash", str(ROOT / "scripts/install-linux.sh"), "--no-start"], env=env)
    assert failed.returncode == 42
    assert (root / "etc/drivecheck/.managed-by-drivecheck").is_file()
    assert (root / "usr/local/sbin/drivecheck-uninstall").is_file()
    subprocess.run(["bash", str(ROOT / "scripts/uninstall-linux.sh")], env=env, check=True)

    env["DRIVECHECK_TEST_ROOT"] = "/"
    rejected = subprocess.run(
        ["bash", str(ROOT / "scripts/install-linux.sh"), "--no-start"], env=env
    )
    assert rejected.returncode != 0

    actual = tmp_path / "actual-root"
    actual.mkdir()
    linked = tmp_path / "linked-root"
    linked.symlink_to(actual, target_is_directory=True)
    env["DRIVECHECK_TEST_ROOT"] = str(linked)
    rejected = subprocess.run(
        ["bash", str(ROOT / "scripts/install-linux.sh"), "--no-start"], env=env
    )
    assert rejected.returncode != 0


def test_shell_scripts_parse():
    subprocess.run(["bash", "-n", str(ROOT / "install.sh")], check=True)
    for name in ("install-linux.sh", "install-pi.sh", "uninstall-linux.sh", "install-macos.sh", "uninstall-macos.sh"):
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "deploy/run-macos.sh")], check=True)
