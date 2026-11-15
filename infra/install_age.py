"""Install checksum-pinned age1.3.2 for images or synthetic CI restore tests."""
import argparse
import hashlib
import io
import platform
import tarfile
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--with-keygen", action="store_true", help="include keygen for synthetic restore tests")
args = parser.parse_args()

digests = {"x86_64": ("amd64", "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10"),
           "aarch64": ("arm64", "6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4")}
arch, expected = digests[platform.machine()]
with urllib.request.urlopen(f"https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-{arch}.tar.gz", timeout=90) as response:
    content = response.read(50 * 1024 * 1024)
if hashlib.sha256(content).hexdigest() != expected:
    raise SystemExit("age checksum mismatch")
with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
    for name in (["age", "age-keygen"] if args.with_keygen else ["age"]):
        target = Path("/usr/local/bin") / name
        target.write_bytes(archive.extractfile("age/" + name).read())
        target.chmod(0o755)
