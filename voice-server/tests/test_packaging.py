import os
import plistlib
import subprocess
import sys
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[1]


def test_installer_syntax():
    subprocess.run(["bash", "-n", str(PROJECT / "packaging/install.sh")], check=True)


def test_plist_generation_preserves_paths_and_token(config, tmp_path):
    directory = tmp_path / "config & voices"
    directory.mkdir()
    data = yaml.safe_load(config.path.read_text())
    data["token_env"] = "MY_VOICE_TOKEN"
    (directory / "config.yaml").write_text(yaml.safe_dump(data))
    token = "secret<&>@@LOG@@"
    (directory / "token").write_text(token + "\n")
    destination = tmp_path / "agent.plist"
    logs = tmp_path / "logs & output"
    installer = (PROJECT / "packaging/install.sh").read_text()
    code = installer.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    command = [sys.executable, "-", str(PROJECT), str(directory), str(destination), str(logs)]
    for _ in range(2):
        subprocess.run(command, input=code, text=True, check=True, capture_output=True)
    plist = plistlib.loads(destination.read_bytes())
    assert plist["EnvironmentVariables"] == {"MY_VOICE_TOKEN": token}
    assert plist["ProgramArguments"] == [
        str(PROJECT / ".venv/bin/python"),
        "-m",
        "familiar_voice_server.supervisor",
        "--config",
        str(directory / "config.yaml"),
    ]
    assert plist["WorkingDirectory"] == str(PROJECT)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["StandardOutPath"] == str(logs / "familiar-voice-server.log")
    assert plist["StandardErrorPath"] == plist["StandardOutPath"]
    assert os.stat(destination).st_mode & 0o777 == 0o600
