from __future__ import annotations

import argparse

from quart.pipeline import run_daily


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily stock-picking signal report")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()
    run_daily(strategy_name=args.strategy, push=not args.no_push)


if __name__ == "__main__":
    main()
