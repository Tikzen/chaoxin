"""Build a distributable without local accounts, keys, logs or virtualenvs."""
import hashlib
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist" / "chaoxin-macos.zip"
OUTPUT.parent.mkdir(exist_ok=True)
files = [ROOT / name for name in subprocess.check_output(
    ["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0") if name]
with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(files):
        archive.write(file, Path("chaoxin") / file.relative_to(ROOT))
with zipfile.ZipFile(OUTPUT) as archive:
    names = archive.namelist()
    assert not any("/.data/" in name or "/.venv/" in name or name.endswith("cookies.txt") for name in names)
    assert "chaoxin/启动课栈.command" in names
    assert archive.getinfo("chaoxin/启动课栈.command").external_attr >> 16 & 0o111
    assert archive.testzip() is None
digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
OUTPUT.with_suffix(".zip.sha256").write_text(f"{digest}  {OUTPUT.name}\n")
print(f"Packaged {len(files)} files: {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")
