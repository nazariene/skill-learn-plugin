"""Download integrity-pinned released source fixtures; never execute package code."""
import base64
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def main():
    root = Path(os.environ.get("OPENCODE_V2_FIXTURES", Path(__file__).parent / ".v2-fixtures")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for fixture in json.loads(Path(__file__).with_name("fixtures").joinpath("v2-sources.json").read_text()):
        with urllib.request.urlopen(fixture["url"], timeout=60) as response:
            archive = response.read()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode()
        if integrity != fixture["integrity"]:
            raise ValueError("Fixture integrity mismatch: " + fixture["version"])
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            staged = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
                for member in package.getmembers():
                    target = staged / member.name
                    if not target.resolve().is_relative_to(staged) or not (member.isfile() or member.isdir()):
                        raise ValueError("Unsafe fixture member: " + member.name)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(package.extractfile(member).read())
            if json.loads((staged / "package/package.json").read_text())["version"] != fixture["version"]:
                raise ValueError("Fixture version mismatch")
            destination = root / fixture["version"]
            if destination.exists():
                shutil.rmtree(destination)
            destination.mkdir()
            shutil.move(staged / "package", destination / "package")
        print(f"Prepared @opencode/core {fixture['version']}")


if __name__ == "__main__":
    main()
