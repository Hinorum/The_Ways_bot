from app.rounds import commit_rule
from app.models import WinRule


def test_rule_commitment_fits_column() -> None:
    salt = "a" * 32
    value = f"{commit_rule(WinRule.MEDIAN, salt)}:{salt}"
    assert 64 < len(value) <= 128
