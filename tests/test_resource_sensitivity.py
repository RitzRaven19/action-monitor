from judge.resource_sensitivity import (
    DEFAULT_SCORE,
    HIGH_SENSITIVITY_SCORE,
    MEDIUM_SENSITIVITY_SCORE,
    sensitivity,
)


def test_high_sensitivity_keywords_score_highest():
    assert sensitivity("data/credentials.txt") == HIGH_SENSITIVITY_SCORE
    assert sensitivity("data/api_key.json") == HIGH_SENSITIVITY_SCORE
    assert sensitivity("secrets/db_password.txt") == HIGH_SENSITIVITY_SCORE


def test_medium_sensitivity_keywords_score_medium():
    assert sensitivity("data/team_roster.txt") == MEDIUM_SENSITIVITY_SCORE
    assert sensitivity("data/employee_salary.csv") == MEDIUM_SENSITIVITY_SCORE


def test_ordinary_resource_scores_default():
    assert sensitivity("data/summary.txt") == DEFAULT_SCORE
    assert sensitivity("data/onboarding_checklist.txt") == DEFAULT_SCORE


def test_case_insensitive():
    assert sensitivity("data/CREDENTIALS.TXT") == HIGH_SENSITIVITY_SCORE


def test_high_beats_medium_when_both_present():
    assert sensitivity("data/employee_password.txt") == HIGH_SENSITIVITY_SCORE
