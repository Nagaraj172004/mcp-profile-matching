"""Simulate a new application arriving while the agent watches resumes/.

    python drop_resume.py              copy samples/resume_ravi_teja.txt into resumes/ after 5 seconds
    python drop_resume.py --delay 0    copy immediately
    python drop_resume.py --remove     delete it again (watch_directory reports it as deleted)

This writes straight to disk on purpose: it stands in for a person or an
upload service adding a file, not for the agent.
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sample", nargs="?", default="resume_ravi_teja.txt")
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    target = ROOT / "resumes" / args.sample
    if args.remove:
        target.unlink(missing_ok=True)
        print("removed", target.relative_to(ROOT).as_posix())
        return
    if args.delay:
        print("dropping {0} into resumes/ in {1:.0f} seconds...".format(args.sample, args.delay))
        time.sleep(args.delay)
    shutil.copyfile(ROOT / "samples" / args.sample, target)
    print("added", target.relative_to(ROOT).as_posix())


if __name__ == "__main__":
    main()
