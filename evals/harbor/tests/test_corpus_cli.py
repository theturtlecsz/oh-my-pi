"""The corpus CLI exposes verification of an existing manifest only."""

import pytest

from omp_harbor_eval.corpus import main


def test_freeze_is_not_a_cli_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["freeze"])

    assert error.value.code == 2
    assert "invalid choice: 'freeze'" in capsys.readouterr().err
