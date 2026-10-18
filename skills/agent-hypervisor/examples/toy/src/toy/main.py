"""CLI entry point: python3 -m toy <a> <b> --op add|mul|power"""

import argparse

from toy import calc


def main(argv=None):
    parser = argparse.ArgumentParser(prog="toy")
    parser.add_argument("a", type=int)
    parser.add_argument("b", type=int)
    parser.add_argument("--op", default="add", choices=["add", "mul", "power"])
    args = parser.parse_args(argv)
    print(getattr(calc, args.op)(args.a, args.b))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
