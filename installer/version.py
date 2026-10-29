"""Prints the tracker's VERSION from hunttracker.py. With --bump, adds 1 to the last number first."""
import re
import sys
from pathlib import Path

src = Path(__file__).resolve().parent.parent / "hunttracker.py"
text = src.read_text(encoding="utf-8")
m = re.search(r'^VERSION = "(\d+)\.(\d+)\.(\d+)"', text, re.M)
if not m:
    sys.exit("VERSION not found in hunttracker.py")
major, minor, patch = map(int, m.groups())
if "--bump" in sys.argv:
    patch += 1
    text = text[:m.start()] + f'VERSION = "{major}.{minor}.{patch}"' + text[m.end():]
    src.write_text(text, encoding="utf-8")
print(f"{major}.{minor}.{patch}")
