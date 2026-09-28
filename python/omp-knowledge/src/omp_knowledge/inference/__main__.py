from __future__ import annotations

from omp_knowledge.errors import KnowledgeError

from .cli import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KnowledgeError as exc:
        raise SystemExit(f"{exc.code}: {exc}")
