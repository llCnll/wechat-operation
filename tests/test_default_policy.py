import pytest
from django.core.management import call_command

from reviews.management.commands.ensure_default_policy import DEFAULT_POLICY_NAME, DEFAULT_RULES
from reviews.models import PolicyRule, PolicyVersion, RecommendedAction


@pytest.mark.django_db
def test_default_policy_command_creates_an_active_editable_baseline():
    call_command("ensure_default_policy")

    policy = PolicyVersion.objects.get(name=DEFAULT_POLICY_NAME)

    assert policy.is_active is True
    assert PolicyRule.objects.filter(policy_version=policy).count() == len(DEFAULT_RULES)
    assert PolicyRule.objects.get(
        policy_version=policy, risk_label="NEGATIVE_SPAM"
    ).recommended_action == RecommendedAction.SINK
    assert PolicyRule.objects.get(
        policy_version=policy, risk_label="TRANSACTION_RISK"
    ).recommended_action == RecommendedAction.HUMAN_REVIEW


@pytest.mark.django_db
def test_default_policy_command_is_idempotent_and_preserves_existing_active_policy():
    existing = PolicyVersion.objects.create(name="现有策略", version="v1", is_active=True)
    call_command("ensure_default_policy")
    call_command("ensure_default_policy")

    default_policy = PolicyVersion.objects.get(name=DEFAULT_POLICY_NAME)

    assert existing.is_active is True
    assert default_policy.is_active is False
    assert PolicyRule.objects.filter(policy_version=default_policy).count() == len(DEFAULT_RULES)
