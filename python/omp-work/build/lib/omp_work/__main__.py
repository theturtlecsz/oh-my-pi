from __future__ import annotations

import argparse
import json

from . import CONTRACT_VERSION, _contract_dir, contract_sha256, generate_schema, validate_bundle


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m omp_work")
    subcommands = parser.add_subparsers(dest="command", required=True)
    schema = subcommands.add_parser("schema")
    schema.add_argument("--check", action="store_true")
    schema.add_argument("--write", action="store_true")
    subcommands.add_parser("hash")
    validate = subcommands.add_parser("validate")
    validate.add_argument("--require-approval", action="store_true")
    args = parser.parse_args()
    if args.command == "schema":
        path = _contract_dir() / "schema.json"
        content = json.dumps(generate_schema(), indent=2, sort_keys=True) + "\n"
        if args.write:
            path.write_text(content)
        if args.check and path.read_text() != content:
            raise SystemExit("schema drift")
        return
    if args.command == "hash":
        print(contract_sha256())
        return
    try:
        validate_bundle(require_approval=args.require_approval)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"{CONTRACT_VERSION} {contract_sha256()} valid")


if __name__ == "__main__":
    main()
