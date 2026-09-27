from __future__ import annotations

import sys

from .cli import main as cli_main
from .web import main as web_main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "serve":
        return web_main(args[1:])
    if args and args[0] == "show":
        return cli_main(args)
    # Default to cli_main (show)
    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main())
