"""Builds docs/demo.gif: one saved case replayed stage by stage. Free: no API calls.

    python docs/make_demo_gif.py eval/runs/<run>/F NG-194

Writes one SVG per stage with triage.replay, screenshots each with headless Chrome, and
joins them with ffmpeg, holding each stage long enough to read. Needs Google Chrome and
ffmpeg; set CHROME to point somewhere else.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from triage.replay import main as replay  # noqa: E402

CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
SECONDS = {"case": 7, "precheck": 5, "triage": 9, "rules": 5, "challenge": 13, "revise": 10, "recheck": 4, "route": 10}


def main(argv: list[str]) -> int:
    run_dir, case_id = argv[:2]
    out = ROOT / "docs" / "demo.gif"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        replay([run_dir, case_id, "--frames", str(tmp / "svg")])
        lines = []
        for svg in sorted((tmp / "svg").glob("*.svg")):
            w, h = (round(float(x)) for x in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text()).groups())
            png = svg.with_suffix(".png")
            subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={tmp / 'profile'}",
                            "--hide-scrollbars", f"--window-size={w},{h}", f"--screenshot={png}", svg.as_uri()],
                           check=True, capture_output=True)
            stage = svg.stem.split("-", 1)[1]
            lines += [f"file '{png}'", f"duration {SECONDS.get(stage, 6)}"]
        lines.append(f"file '{png}'")  # ffmpeg's concat demuxer ignores the last duration without this
        (tmp / "list.txt").write_text("\n".join(lines) + "\n")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"), "-fps_mode", "vfr",
                        "-vf", "scale=1000:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse",
                        "-loop", "0", str(out)], check=True)
    print(f"Wrote {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
