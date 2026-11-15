"""Download only the audited release; fail closed on architecture/checksum mismatch."""
import hashlib
import io
import json
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

manifest = json.loads(Path("wacli-release.json").read_text())
arch = os.environ.get("TARGETARCH") or {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
if arch not in {"amd64", "arm64"}:
    raise SystemExit("Unsupported wacli architecture")
name = f"wacli_{manifest['version']}_linux_{arch}.tar.gz"
url = f"{manifest['repository']}/releases/download/{manifest['tag']}/{name}"
with urllib.request.urlopen(url, timeout=120) as response:
    archive = response.read(100 * 1024 * 1024)
if hashlib.sha256(archive).hexdigest() != manifest[f"linux_{arch}_sha256"]:
    raise SystemExit("wacli release checksum mismatch")
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
    binary = tar.extractfile("wacli")
    if binary is None:
        raise SystemExit("release binary missing")
    target = Path("/usr/local/bin/wacli")
    target.write_bytes(binary.read())
    target.chmod(0o755)
