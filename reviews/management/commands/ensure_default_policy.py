from django.core.management.base import BaseCommand

from reviews.models import PolicyRule, PolicyVersion, RecommendedAction

DEFAULT_POLICY_NAME = "通用内容审核策略"
DEFAULT_POLICY_VERSION = "v1"
DEFAULT_RULES = (
    ("PRIVATE_SERVER", 0.85, RecommendedAction.BLOCK, 95),
    ("FRAUD_SCAM", 0.90, RecommendedAction.BLOCK, 95),
    ("PORNOGRAPHY", 0.90, RecommendedAction.BLOCK, 95),
    ("POLITICAL_SENSITIVE", 0.90, RecommendedAction.BLOCK, 95),
    ("TRAFFIC_DIVERSION", 0.85, RecommendedAction.BLOCK, 90),
    ("CHEAT", 0.85, RecommendedAction.BLOCK, 90),
    ("ADVERTISEMENT_SPAM", 0.85, RecommendedAction.BLOCK, 85),
    ("PERSONAL_ATTACK", 0.85, RecommendedAction.BLOCK, 80),
    ("TRANSACTION_RISK", 0.85, RecommendedAction.HUMAN_REVIEW, 75),
    ("OFF_TOPIC", 0.85, RecommendedAction.BLOCK, 70),
    ("NEGATIVE_SPAM", 0.80, RecommendedAction.SINK, 65),
    ("CONTENT_ANOMALY", 0.80, RecommendedAction.HUMAN_REVIEW, 60),
)


class Command(BaseCommand):
    help = "初始化可编辑的通用内容审核策略，不覆盖已有策略或规则。"

    def handle(self, *args, **options):
        policy, created = PolicyVersion.objects.get_or_create(
            name=DEFAULT_POLICY_NAME,
            defaults={"version": DEFAULT_POLICY_VERSION},
        )
        if not PolicyVersion.objects.filter(is_active=True).exists() and not policy.is_active:
            policy.is_active = True
            policy.save(update_fields=("is_active",))

        created_rules = 0
        for risk_label, min_confidence, recommended_action, priority in DEFAULT_RULES:
            if PolicyRule.objects.filter(policy_version=policy, risk_label=risk_label).exists():
                continue
            PolicyRule.objects.create(
                policy_version=policy,
                risk_label=risk_label,
                min_confidence=min_confidence,
                recommended_action=recommended_action,
                priority=priority,
            )
            created_rules += 1

        state = "已创建" if created else "已保留"
        activation = "已启用" if policy.is_active else "未启用（已有其他启用策略）"
        self.stdout.write(
            f"{state}默认策略“{policy.name} {policy.version}”，"
            f"新增 {created_rules} 条规则，{activation}。"
        )
