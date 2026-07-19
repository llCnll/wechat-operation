"""Small, read-only context used by the system configuration homepage."""

from django.db.models import Max


def admin_console_overview(request):
    """Avoid loading operational summaries outside the configuration console."""
    if (
        request.resolver_match is None
        or request.resolver_match.namespace != "admin"
        or request.resolver_match.url_name != "index"
    ):
        return {}

    from jobs.models import BackgroundJob, JobStatus
    from posts.models import ManagedGame
    from reviews.models import PolicyVersion, risk_label_display

    active_policies = list(PolicyVersion.objects.filter(is_active=True).order_by("-created_at"))
    active_policy = active_policies[0] if len(active_policies) == 1 else None
    active_policy_rules = []
    if active_policy:
        active_policy_rules = [
            {
                "risk_label": risk_label_display(rule.risk_label),
                "min_confidence": f"{rule.min_confidence:.0%}",
                "recommended_action": rule.get_recommended_action_display(),
                "action_code": rule.recommended_action.lower(),
                "priority": rule.priority,
            }
            for rule in active_policy.rules.filter(enabled=True).order_by("-priority", "risk_label")
        ]
    enabled_rule_count = len(active_policy_rules)
    if not active_policies:
        policy_status = {
            "label": "未启用策略",
            "description": "AI 评估不会有可追溯的启用策略版本。",
            "tone": "is-alert",
        }
    elif len(active_policies) > 1:
        policy_status = {
            "label": "多个策略同时启用",
            "description": f"检测到 {len(active_policies)} 个启用版本，请保留唯一生效版本。",
            "tone": "is-alert",
        }
    elif not enabled_rule_count:
        policy_status = {
            "label": "启用策略没有规则",
            "description": "请新增并启用至少一条规则后再依赖策略建议。",
            "tone": "is-warning",
        }
    else:
        policy_status = {
            "label": "策略有效",
            "description": f"当前启用策略包含 {enabled_rule_count} 条规则。",
            "tone": "is-healthy",
        }
    game_summary = ManagedGame.objects.aggregate(last_synced_at=Max("last_synced_at"))
    return {
        "admin_console": {
            "active_policy": active_policy,
            "active_policy_rules": active_policy_rules,
            "policy_status": policy_status,
            "managed_game_count": ManagedGame.objects.count(),
            "last_game_sync_at": game_summary["last_synced_at"],
            "pending_job_count": BackgroundJob.objects.filter(status=JobStatus.PENDING).count(),
            "running_job_count": BackgroundJob.objects.filter(status=JobStatus.RUNNING).count(),
            "failed_job_count": BackgroundJob.objects.filter(status=JobStatus.FAILED).count(),
        }
    }
