from __future__ import annotations

from typing import Iterable
from uuid import UUID, uuid4

import psycopg

from omp_work.knowledge_contracts import (
    RepositoryAlias,
    RepositoryAliasKind,
    RepositoryBinding,
    RepositoryBindingState,
)


class ScopeViolationError(Exception):
    pass


def resolve_repository_binding(
    conn: psycopg.Connection,
    candidate_aliases: Iterable[RepositoryAlias],
) -> RepositoryBinding:
    """Resolve repository identity from evidence aliases.
    If verified aliases bind to an existing repository, return its binding.
    Otherwise create an unbound repository identity.
    Derived repository aliases never override native identity acceptance.
    """
    aliases_list = list(candidate_aliases)
    with conn.cursor() as cur:
        for alias in aliases_list:
            cur.execute(
                """
                SELECT ra.repository_id, r.native_repository_id, r.state
                FROM omp_knowledge.repository_aliases ra
                JOIN omp_knowledge.repositories r ON r.repository_id = ra.repository_id
                WHERE ra.kind = %s AND ra.value = %s
                """,
                (alias.kind.value, alias.value),
            )
            row = cur.fetchone()
            if row:
                repo_id = row["repository_id"]
                native_id = row["native_repository_id"]
                state = RepositoryBindingState(row["state"])
                return RepositoryBinding(
                    repository_id=repo_id,
                    native_repository_id=native_id,
                    state=state,
                    aliases=tuple(aliases_list),
                )

        # No match found -> create an unbound repository
        new_repo_id = uuid4()
        cur.execute(
            """
            INSERT INTO omp_knowledge.repositories (repository_id, native_repository_id, state)
            VALUES (%s, NULL, %s)
            """,
            (new_repo_id, RepositoryBindingState.UNBOUND.value),
        )
        for alias in aliases_list:
            cur.execute(
                """
                INSERT INTO omp_knowledge.repository_aliases (
                    alias_id, repository_id, kind, value, verified_by, verified_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (kind, value) DO NOTHING
                """,
                (
                    uuid4(),
                    new_repo_id,
                    alias.kind.value,
                    alias.value,
                    alias.verified_by,
                    alias.verified_at,
                ),
            )
        conn.commit()
        return RepositoryBinding(
            repository_id=new_repo_id,
            native_repository_id=None,
            state=RepositoryBindingState.UNBOUND,
            aliases=tuple(aliases_list),
        )


def bind_native_repository(
    conn: psycopg.Connection,
    *,
    repository_id: UUID,
    native_repository_id: UUID,
) -> RepositoryBinding:
    """Explicit binding of a repository to a native repository UUID."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE omp_knowledge.repositories
                SET native_repository_id = %s, state = %s
                WHERE repository_id = %s
                """,
                (native_repository_id, RepositoryBindingState.BOUND.value, repository_id),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.repositories (repository_id, native_repository_id, state)
                    VALUES (%s, %s, %s)
                    """,
                    (repository_id, native_repository_id, RepositoryBindingState.BOUND.value),
                )
    return RepositoryBinding(
        repository_id=repository_id,
        native_repository_id=native_repository_id,
        state=RepositoryBindingState.BOUND,
    )


def assert_retrieval_allowed(
    binding: RepositoryBinding,
    target_repository_id: UUID,
) -> None:
    """Enforce that unbound repositories cannot participate in cross-repository retrieval."""
    if binding.repository_id != target_repository_id:
        if binding.state != RepositoryBindingState.BOUND:
            raise ScopeViolationError(
                f"Cross-repository retrieval forbidden for unbound repository {binding.repository_id}"
            )
