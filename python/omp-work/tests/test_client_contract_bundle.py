"""OMP-416-s01-s06: client contract bundle validation tests."""

from __future__ import annotations

import pytest

from omp_work import load_contract, validate_client_contract


def test_validate_client_contract_returns_none() -> None:
    contract = load_contract()
    assert validate_client_contract(contract) is None


def test_duplicate_operation_name_raises() -> None:
    contract = load_contract()
    ops = list(contract.client_contract.operations)
    bad_op = ops[1].model_copy(update={"name": ops[0].name})
    new_ops = (ops[0], bad_op, *ops[2:])
    bad_contract = contract.model_copy(
        update={
            "client_contract": contract.client_contract.model_copy(
                update={"operations": new_ops}
            )
        }
    )
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)


def test_duplicate_method_path_raises() -> None:
    contract = load_contract()
    ops = list(contract.client_contract.operations)
    bad_op = ops[1].model_copy(update={"path": ops[0].path})
    new_ops = (ops[0], bad_op, *ops[2:])
    bad_contract = contract.model_copy(
        update={
            "client_contract": contract.client_contract.model_copy(
                update={"operations": new_ops}
            )
        }
    )
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)


def test_distinct_methods_can_share_path() -> None:
    contract = load_contract()
    stop_get = next(
        op for op in contract.client_contract.operations if op.name == "stop.status"
    )
    stop_post = next(
        op for op in contract.client_contract.operations if op.name == "stop.engage"
    )
    assert stop_get.path == stop_post.path
    assert stop_get.method != stop_post.method
    assert validate_client_contract(contract) is None


def test_get_path_not_in_reads_raises() -> None:
    contract = load_contract()
    ops = list(contract.client_contract.operations)
    bad_op = ops[0].model_copy(
        update={"path": "/v1/workspaces/{workspace_id}/client/unregistered_path"}
    )
    new_ops = (bad_op, *ops[1:])
    bad_contract = contract.model_copy(
        update={
            "client_contract": contract.client_contract.model_copy(
                update={"operations": new_ops}
            )
        }
    )
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)


def test_get_with_command_raises() -> None:
    contract = load_contract()
    ops = list(contract.client_contract.operations)
    bad_op = ops[0].model_copy(update={"command": "relay_owner_intent"})
    new_ops = (bad_op, *ops[1:])
    bad_contract = contract.model_copy(
        update={
            "client_contract": contract.client_contract.model_copy(
                update={"operations": new_ops}
            )
        }
    )
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)


def test_get_with_request_raises() -> None:
    contract = load_contract()
    ops = list(contract.client_contract.operations)
    bad_op = ops[0].model_copy(update={"request": "UnexpectedPayload"})
    new_ops = (bad_op, *ops[1:])
    bad_contract = contract.model_copy(
        update={
            "client_contract": contract.client_contract.model_copy(
                update={"operations": new_ops}
            )
        }
    )
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)


def test_post_unknown_command_type_raises() -> None:
    contract = load_contract()
    ops = list(contract.client_contract.operations)
    post_idx = next(i for i, op in enumerate(ops) if op.method == "POST")
    bad_op = ops[post_idx].model_copy(update={"command": "unknown_post_command"})
    new_ops = tuple(bad_op if i == post_idx else op for i, op in enumerate(ops))
    bad_contract = contract.model_copy(
        update={
            "client_contract": contract.client_contract.model_copy(
                update={"operations": new_ops}
            )
        }
    )
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)


def test_post_with_empty_request_raises() -> None:
    contract = load_contract()
    ops = list(contract.client_contract.operations)
    post_idx = next(i for i, op in enumerate(ops) if op.method == "POST")
    bad_op = ops[post_idx].model_copy(update={"request": ""})
    new_ops = tuple(bad_op if i == post_idx else op for i, op in enumerate(ops))
    bad_contract = contract.model_copy(
        update={
            "client_contract": contract.client_contract.model_copy(
                update={"operations": new_ops}
            )
        }
    )
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)


def test_post_with_none_request_raises() -> None:
    contract = load_contract()
    ops = list(contract.client_contract.operations)
    post_idx = next(i for i, op in enumerate(ops) if op.method == "POST")
    bad_op = ops[post_idx].model_copy(update={"request": None})
    new_ops = tuple(bad_op if i == post_idx else op for i, op in enumerate(ops))
    bad_contract = contract.model_copy(
        update={
            "client_contract": contract.client_contract.model_copy(
                update={"operations": new_ops}
            )
        }
    )
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)


def test_unknown_scope_raises() -> None:
    contract = load_contract()
    ops = list(contract.client_contract.operations)
    bad_op = ops[0].model_copy(update={"scope": ("work.read", "work.unknown_scope")})
    new_ops = (bad_op, *ops[1:])
    bad_contract = contract.model_copy(
        update={
            "client_contract": contract.client_contract.model_copy(
                update={"operations": new_ops}
            )
        }
    )
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)


def test_client_scopes_without_work_stop_raises() -> None:
    contract = load_contract()
    bad_policy = contract.security_policy.model_copy(
        update={"client_scopes": ("work.read", "work.client")}
    )
    bad_contract = contract.model_copy(update={"security_policy": bad_policy})
    with pytest.raises(ValueError, match="client contract closure failed"):
        validate_client_contract(bad_contract)
