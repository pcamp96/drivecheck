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
    command(commands / "dpkg-query", "exit 1")
    command(commands / "apt-get", f'echo "apt-get $*" >> "{log}"')
    command(commands / "systemctl", f'echo "systemctl $*" >> "{log}"; exit 0')
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
    assert "DRIVECHECK_ALLOW_DESTRUCTIVE=false" in initial
    assert (root / "opt/drivecheck/LICENSE").is_file()
    assert (root / "opt/drivecheck/README.md").is_file()

    config.write_text("DRIVECHECK_HEADLESS=true\n")
    data.write_text("keep me\n")
    subprocess.run(
        ["bash", str(ROOT / "scripts/install-linux.sh"), "--no-start"], env=env, check=True
    )
    assert config.read_text() == "DRIVECHECK_HEADLESS=true\n"
    assert data.read_text() == "keep me\n"
    assert (root / "usr/local/sbin/drivecheck-uninstall").is_file()

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


def test_shell_scripts_parse():
    for name in ("install-linux.sh", "install-pi.sh", "uninstall-linux.sh"):
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)
