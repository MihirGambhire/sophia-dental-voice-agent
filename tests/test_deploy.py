"""
Checks on the deployment files, which nothing else would exercise until a
deploy failed on the host.
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==(\S+)", line.strip())
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def test_the_deployed_server_uses_the_same_versions_as_development():
    dev = _pins(ROOT / "requirements.txt")
    deploy = _pins(ROOT / "requirements-deploy.txt")
    assert deploy, "no pinned requirements for deployment"
    for name, version in deploy.items():
        assert dev.get(name) == version, f"{name} is {version} for deploy but {dev.get(name)} in development"


def test_the_server_imports_without_the_webrtc_libraries():
    """The deploy install leaves WebRTC out, so importing the app must not need it."""
    code = (
        "import builtins, sys\n"
        "real = builtins.__import__\n"
        "def blocked(name, *a, **k):\n"
        "    if name.startswith(('aiortc', 'pipecat.transports.smallwebrtc')):\n"
        "        raise ImportError(name)\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = blocked\n"
        "sys.path.insert(0, 'src')\n"
        "from sophia import voice_app\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-W", "ignore", "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert result.stdout.strip().endswith("ok"), result.stderr[-800:]


def test_the_blueprint_keeps_secrets_out_of_the_file():
    service = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))["services"][0]
    assert service["plan"] == "free"
    assert service["healthCheckPath"] == "/api/health"
    for variable in service["envVars"]:
        if variable["key"].endswith("_API_KEY"):
            assert variable.get("sync") is False and "value" not in variable, variable["key"]


def test_the_image_never_contains_the_env_file():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored
