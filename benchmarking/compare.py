"""Compare two benchmark results, for example before and after a change to the server.

    python -m benchmarking.compare reports/before reports/after
    python -m benchmarking.compare benchmarking/snapshots/2026-11-11_before-tool-cleanup \
        benchmarking/snapshots/2026-11-11_after-tool-cleanup \
        --labels "22 tools" "19 tools" --output comparison.md

Each side is a benchmark-*.json or a folder that is searched for them (one subfolder per
model is fine). The comparison covers the models, tool profiles and cases both sides ran.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarking.reporters.comparison import comparison_markdown, load_results  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two benchmark results.")
    parser.add_argument("before", help="benchmark-*.json or a folder with such files")
    parser.add_argument("after", help="benchmark-*.json or a folder with such files")
    parser.add_argument("--labels", nargs=2, default=["before", "after"], metavar=("BEFORE", "AFTER"))
    parser.add_argument("--output", default="", help="write the Markdown here instead of printing it")
    args = parser.parse_args(argv)
    try:
        before, before_metas = load_results(args.before)
        after, after_metas = load_results(args.after)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    markdown = comparison_markdown(before, after, before_metas, after_metas, labels=(args.labels[0], args.labels[1]))
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
        print(f"comparison: {args.output}", file=sys.stderr)
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
