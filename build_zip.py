from __future__ import annotations
import os
import sys
import time
import zipfile
from pathlib import Path

root = Path.cwd()
out = root / "veda_source.zip"

skip_dirs = {
    ".venv", ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".eggs", ".tox", ".uv", "__pycache__", "transcripts", "data",
    ".pending_calls",
}

count = 0
total = 0
t0 = time.time()
progress_every = 50

with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
    for dirpath, dirs, files in os.walk(root, topdown=True):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        rel_root = Path(dirpath)
        for name in files:
            if name.startswith("."):
                continue
            if name.endswith((".pyc", ".pyo", ".egg-info")):
                continue
            p = rel_root / name
            if not p.is_file():
                continue
            arc = p.relative_to(root)
            if arc.parts and arc.parts[0] in skip_dirs:
                continue
            try:
                z.write(p, arc)
            except Exception as exc:
                print(f"skip {p}: {exc}", file=sys.stderr)
                continue
            count += 1
            total += p.stat().st_size
            if count % progress_every == 0:
                elapsed = time.time() - t0
                rate = total / 1024 / 1024 / max(elapsed, 0.001)
                print(f"progress: {count} files, {total/1024/1024:.1f} MB, {rate:.1f} MB/s, {elapsed:.1f}s", file=sys.stderr)

elapsed = time.time() - t0
print(f"written={count} bytes={total} kb={total/1024:.1f} time={elapsed:.1f}s")
print(f"out={out}")
print(f"size_kb={out.stat().st_size/1024:.1f}")

# Integrity check
print("checking integrity...", file=sys.stderr)
z2 = zipfile.ZipFile(out)
bad = z2.testzip()
names = z2.namelist()
print(f"integrity: bad={bad} members={len(names)}", file=sys.stderr)
if bad:
    print(f"FIRST BAD: {bad}", file=sys.stderr)
    sys.exit(1)
print(f"first5={names[:5]}", file=sys.stderr)
print(f"last5={names[-5:]}", file=sys.stderr)
