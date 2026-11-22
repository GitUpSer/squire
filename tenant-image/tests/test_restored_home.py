"""Exercise the real entrypoint's explicit founder adoption boundary."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

IMAGE_ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/test/founder@sha256:" + "a" * 64


@pytest.fixture
def restored(tmp_path):
    home = tmp_path / "volume"
    contents = {"config.yaml": b"memory:\n  provider: hindsight\n",
                "SOUL.md": b"Founder custom identity\n",
                "hindsight/config.json": b'{"bank_id":"founder-original-bank","mode":"local_external"}\n',
                "plugins/hindsight/__init__.py": b"# founder reviewed plugin\n"}
    for name, body in contents.items():
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    (home / "state.db").write_bytes(b"restored source database sentinel")
    (home / ".env").write_text("ANTHROPIC_API_KEY=sk-restored-founder-test\n")
    state = home / ".squire"
    state.mkdir()
    manifest = {"format": "squire-restored-home-v1", "runtime_id": "founder-test",
                "image_ref": IMAGE, "migration_id": "reviewed-test",
                "files": {name: hashlib.sha256(body).hexdigest() for name, body in contents.items()}}
    (state / "restored-home.json").write_text(json.dumps(manifest))
    template = tmp_path / "template"
    shutil.copytree(IMAGE_ROOT / "home-template", template)
    (template / "plugins/hindsight").mkdir(parents=True, exist_ok=True)
    (template / "plugins/hindsight/new-version-only.py").write_text("# must not mix into restored plugin\n")
    env = {"PATH": str(Path(sys.executable).parent) + ":" + os.environ["PATH"],
           "HOME": str(home), "HERMES_HOME": str(home), "SQUIRE_VOLUME": str(home),
           "SQUIRE_ALLOW_EPHEMERAL_VOLUME": "1", "SQUIRE_STATE_DIR": str(state),
           "SQUIRE_SECRETS_TMPFS": str(tmp_path / "shm"),
           "SQUIRE_HOME_TEMPLATE": str(template),
           "SQUIRE_PYTHON": sys.executable, "SQUIRE_BIN": str(IMAGE_ROOT / "bin"),
           "SQUIRE_SUPERVISORD": "/bin/true", "SQUIRE_SUPERVISORD_CONF": "/dev/null",
           "SQUIRE_DEK": "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
           "TENANT_ID": "founder-test", "SQUIRE_IMAGE_REF": IMAGE,
           "SQUIRE_RESTORED_HOME": "true", "SQUIRE_HINDSIGHT_VARIANT": "founder",
           "SQUIRE_RUNTIME_KIND": "dm", "HERMES_SKIP_CONFIG_MIGRATION": "1"}
    return home, contents, env


def boot(env):
    return subprocess.run(["bash", str(IMAGE_ROOT / "bin/squire-entrypoint.sh")],
                          env=env, capture_output=True, text=True, timeout=30)


def test_adoption_preserves_custom_state_and_seals_credentials(restored):
    home, contents, env = restored
    result = boot(env)
    assert result.returncode == 0, result.stdout + result.stderr
    for name, body in contents.items():
        assert (home / name).read_bytes() == body
    assert not (home / "plugins/hindsight/new-version-only.py").exists()
    assert (home / ".squire/initialized").is_file()
    assert not (home / ".squire/concierge-state.json").exists()
    assert (home / ".env").is_symlink()
    assert (home / ".squire/secrets/.env.enc").is_file()
    assert "sk-restored-founder-test" not in result.stdout + result.stderr
    # After adoption, normal edits are legitimate and do not replay first boot.
    (home / "SOUL.md").write_text("New owner personalization\n")
    assert boot(env).returncode == 0
    assert (home / "SOUL.md").read_text() == "New owner personalization\n"
    assert not (home / "plugins/hindsight/new-version-only.py").exists()
    (home / ".squire/initialized").unlink()
    assert boot(env).returncode != 0
    assert (home / "SOUL.md").read_text() == "New owner personalization\n"


@pytest.mark.parametrize("case", ["flag", "manifest", "group", "image", "changed", "symlink", "migration"])
def test_unreviewed_adoption_fails_before_template_or_services(restored, case):
    home, contents, env = restored
    if case == "flag": env.pop("SQUIRE_RESTORED_HOME")
    if case == "manifest": (home / ".squire/restored-home.json").unlink()
    if case == "group": env["SQUIRE_RUNTIME_KIND"] = "group"
    if case == "image": env["SQUIRE_IMAGE_REF"] = IMAGE.replace("a" * 64, "b" * 64)
    if case == "changed": (home / "SOUL.md").write_text("Unreviewed content\n")
    if case == "migration": env.pop("HERMES_SKIP_CONFIG_MIGRATION")
    if case == "symlink":
        target = home.parent / "outside-soul"
        target.write_bytes(contents["SOUL.md"])
        (home / "SOUL.md").unlink()
        (home / "SOUL.md").symlink_to(target)
    before = (home / "SOUL.md").read_bytes()
    result = boot(env)
    assert result.returncode != 0
    assert "starting supervisord" not in result.stdout
    assert not (home / ".squire/initialized").exists()
    assert (home / "SOUL.md").read_bytes() == before
    assert (home / "hindsight/config.json").read_bytes() == contents["hindsight/config.json"]
