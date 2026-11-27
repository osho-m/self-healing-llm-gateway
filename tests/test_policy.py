import os

import pytest
import yaml
from pydantic import ValidationError

from app.policy import Policy, PolicyManager, hash_key
from tests.conftest import BASE_POLICY, policy_dict


def test_plaintext_keys_are_hashed_on_load():
    policy = Policy.model_validate(BASE_POLICY)
    team = policy.teams[0]
    assert team.api_key_sha256 == [hash_key("alpha-key")]
    assert "alpha-key" not in policy.model_dump_json()


@pytest.mark.parametrize(
    "changes",
    [
        {"tiers": {"balanced": ["missing"]}},
        {"tiers": {"balanced": []}},
        {"teams": [{"team_id": "a", "api_keys": ["k"], "allowed_tiers": ["nope"]}]},
        {
            "teams": [
                {"team_id": "a", "api_keys": ["k"], "allowed_tiers": []},
                {"team_id": "b", "api_keys": ["k"], "allowed_tiers": []},
            ]
        },
    ],
)
def test_invalid_policies_rejected(changes):
    with pytest.raises(ValidationError):
        Policy.model_validate(policy_dict(**changes))


def test_hot_reload_and_invalid_file_keeps_last_good(policy_file):
    path = policy_file()
    mgr = PolicyManager(path)
    assert mgr.team("alpha").requests_per_minute == 1000

    data = policy_dict()
    data["teams"][0]["requests_per_minute"] = 7
    path.write_text(yaml.safe_dump(data))
    os.utime(path, (os.path.getatime(path), os.path.getmtime(path) + 5))
    assert mgr.reload_if_changed() is True
    assert mgr.team("alpha").requests_per_minute == 7

    path.write_text("providers: [")  # broken YAML
    os.utime(path, (os.path.getatime(path), os.path.getmtime(path) + 10))
    assert mgr.reload_if_changed() is False
    assert mgr.team("alpha").requests_per_minute == 7


def test_overrides_take_precedence_and_survive_reload(policy_file):
    path = policy_file()
    mgr = PolicyManager(path)
    mgr.set_overrides({"alpha": {"daily_budget_usd": 1.5}})
    assert mgr.team("alpha").daily_budget_usd == 1.5
    data = policy_dict()
    data["teams"][0]["daily_budget_usd"] = 50
    path.write_text(yaml.safe_dump(data))
    os.utime(path, (os.path.getatime(path), os.path.getmtime(path) + 5))
    mgr.reload_if_changed()
    assert mgr.team("alpha").daily_budget_usd == 1.5
    mgr.set_overrides({})
    assert mgr.team("alpha").daily_budget_usd == 50


def test_override_validation(policy_file):
    mgr = PolicyManager(policy_file())
    with pytest.raises(ValueError):
        mgr.validate_override("alpha", {"role": "admin"})
    with pytest.raises(ValueError):
        mgr.validate_override("alpha", {"allowed_tiers": ["ghost"]})
    mgr.validate_override("alpha", {"requests_per_minute": 3})


def test_example_policy_is_valid():
    PolicyManager("config/policy.example.yml")


def test_production_requires_admin_key_and_redis():
    from app.settings import Settings

    with pytest.raises(ValidationError):
        Settings(environment="production", admin_api_key=None, redis_url="redis://x")
    with pytest.raises(ValidationError):
        Settings(environment="production", admin_api_key="x" * 32, redis_url=None)
    Settings(environment="production", admin_api_key="x" * 32, redis_url="redis://x")


def test_production_rejects_placeholder_admin_keys():
    from app.settings import Settings

    for key in ("demo-admin-key-change-me-0000", "x" * 20 + "-changeme-xxxxx", "example-admin-key-0123456789"):
        with pytest.raises(ValidationError, match="placeholder"):
            Settings(environment="production", admin_api_key=key, redis_url="redis://x")


def test_production_forbids_plaintext_api_keys(policy_file):
    path = policy_file()  # BASE_POLICY uses plaintext api_keys
    with pytest.raises(ValueError, match="plaintext api_keys"):
        PolicyManager(path, forbid_plaintext_keys=True)

    hashed = policy_dict()
    for team in hashed["teams"]:
        team["api_key_sha256"] = [hash_key(k) for k in team.pop("api_keys")]
    path.write_text(yaml.safe_dump(hashed))
    mgr = PolicyManager(path, forbid_plaintext_keys=True)
    assert mgr.team_by_key("alpha-key").team_id == "alpha"

    # A hot reload that reintroduces plaintext keys is rejected; the last good policy stays.
    path.write_text(yaml.safe_dump(policy_dict()))
    os.utime(path, (os.path.getatime(path), os.path.getmtime(path) + 5))
    assert mgr.reload_if_changed() is False
    assert mgr.team_by_key("alpha-key").team_id == "alpha"


def test_production_app_rejects_injected_policy_without_plaintext_guard(policy_file):
    from app.main import create_app
    from app.settings import Settings

    settings = Settings(
        environment="production",
        admin_api_key="k" * 32,
        redis_url="redis://x",
        audit_log_path=None,
        health_checks_enabled=False,
    )
    with pytest.raises(ValueError, match="forbid_plaintext_keys"):
        create_app(settings, policy=PolicyManager(policy_file()))
