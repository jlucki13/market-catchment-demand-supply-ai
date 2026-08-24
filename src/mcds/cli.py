"""Command-line entry point.

After `pip install -e .` the console script is on the path, which is the
invocation that behaves identically across macOS, Linux, and Windows:

    mcds examples/sample_deal.yaml --dry-run
    mcds examples/sample_deal.yaml --out out/

`python -m mcds.cli ...` is equivalent. Without installing, the package under
src/ has to be put on the path by hand, and that syntax is shell-specific --
see the README.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_deal, load_settings
from .pipeline import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcds",
        description="Market catchment demand/supply analysis for one deal.",
    )
    parser.add_argument("deal", help="Path to a deal input YAML or JSON file")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run against fixtures with no network calls and no API keys.",
    )
    parser.add_argument("--out", type=Path, help="Directory to write memo and scorecard into")
    parser.add_argument("--settings", type=Path, help="Path to a settings file")
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Run with no model calls and no Anthropic spend. Substitutability "
             "falls back to vertical-config category priors and the synthesis "
             "and review stages are skipped.",
    )
    parser.add_argument(
        "--no-strict-provenance", action="store_true",
        help="Report provenance violations instead of refusing to render. "
             "Intended for debugging a prompt, not for producing a memo.",
    )
    args = parser.parse_args(argv)

    deal = load_deal(args.deal)
    settings = load_settings(args.settings) if args.settings else None
    result = run(
        deal,
        dry_run=args.dry_run,
        settings=settings,
        strict_provenance=not args.no_strict_provenance,
        use_llm=not args.no_llm,
    )

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{deal['deal_id']}.md").write_text(result.memo_markdown)
        (args.out / f"{deal['deal_id']}.scorecard.json").write_text(
            json.dumps(result.scorecard, indent=2)
        )
        print(f"Wrote {args.out / (deal['deal_id'] + '.md')}", file=sys.stderr)
    else:
        print(result.memo_markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
