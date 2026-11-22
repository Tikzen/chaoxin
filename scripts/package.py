"""Build a distributable without local accounts, keys, logs or virtualenvs."""
import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist" / "chaoxin-macos.zip"
OUTPUT.parent.mkdir(exist_ok=True)
roots = ["app", "static", "scripts", "vendor", "tests", "docs", ".github"]
files = [ROOT / name for name in ["README.md", "LICENSE", "pyproject.toml", "uv.lock", ".gitignore",
          "run.py", "启动课栈.command"]]
for name in roots:
    files.extend(path for path in (ROOT / name).rglob("*") if path.is_file()
                 and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".log")
                 and path.name not in {"windows-startup.ps1", "preview.png"})
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
