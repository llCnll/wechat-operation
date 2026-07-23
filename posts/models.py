from django.db import models

POST_STATUS_NORMAL = 2
POST_STATUS_BLOCKED = 4
POST_STATUS_SUNK = 8
POST_STATUS_SELF_VISIBLE = 16
POST_STATUS_DELETED = 128
POST_STATUS_MODERATION_MASK = POST_STATUS_BLOCKED | POST_STATUS_SUNK


def normalize_post_status(value: object) -> int:
    """Use the normal base status when an upstream response omits its status."""
    try:
        return int(value) if value is not None else POST_STATUS_NORMAL
    except (TypeError, ValueError):
        return POST_STATUS_NORMAL


def has_post_status(status: int | None, flag: int) -> bool:
    return status is not None and (status & flag) != 0


def is_post_deleted(status: int | None) -> bool:
    return has_post_status(status, POST_STATUS_DELETED)


def is_post_blocked(status: int | None) -> bool:
    return has_post_status(status, POST_STATUS_BLOCKED)


def is_post_sunk(status: int | None) -> bool:
    return has_post_status(status, POST_STATUS_SUNK)


def is_post_self_visible(status: int | None) -> bool:
    """Status 16 is an upstream visibility state, not a local moderation action."""
    return status == POST_STATUS_SELF_VISIBLE


def is_post_normal(status: int | None) -> bool:
    return status == POST_STATUS_NORMAL


def mark_post_deleted(status: int | None) -> int:
    """Preserve only upstream moderation flags when adding the local deletion flag."""
    return (normalize_post_status(status) & POST_STATUS_MODERATION_MASK) | POST_STATUS_DELETED


class OriginType(models.TextChoices):
    CLASSIFICATION_PENDING = "CLASSIFICATION_PENDING", "待判定"
    ORIGINAL = "ORIGINAL", "原创"
    FORWARD = "FORWARD", "转发"


class MediaType(models.TextChoices):
    IMAGE = "IMAGE", "图片"
    VIDEO = "VIDEO", "视频"


class ManagedGame(models.Model):
    """A locally cached game the current operations account can manage."""

    appid = models.CharField(max_length=128, unique=True)
    name = models.CharField(max_length=255)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "appid")
        verbose_name = "可管理游戏"
        verbose_name_plural = "可管理游戏"

    def __str__(self) -> str:
        return f"{self.name} ({self.appid})"


class ActiveGameScope(models.Model):
    """The single game currently authorized for upstream operations."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    active_game = models.ForeignKey(
        ManagedGame,
        to_field="appid",
        db_column="active_appid",
        on_delete=models.PROTECT,
        related_name="active_scopes",
    )
    scope_version = models.PositiveBigIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_active_game_scopes",
    )

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="active_game_scope_one")]
        verbose_name = "当前操作游戏"
        verbose_name_plural = "当前操作游戏"

    @property
    def active_appid(self) -> str:
        return self.active_game_id


class GameCircleLoginRuntimeState(models.Model):
    """Credential-free projection written only by the session service."""

    class OuterState(models.TextChoices):
        UNKNOWN = "UNKNOWN", "待检查"
        BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE", "Chrome 不可用"
        BOOTSTRAPPING = "BOOTSTRAPPING", "正在检查"
        LOGIN_PENDING = "LOGIN_PENDING", "需要扫码"
        OUTER_READY = "OUTER_READY", "外层会话有效"

    class FrameState(models.TextChoices):
        UNKNOWN = "UNKNOWN", "未知"
        UNREADY = "UNREADY", "未就绪"
        BUILDING = "BUILDING", "建立中"
        READY = "READY", "有效"

    class Action(models.TextChoices):
        CHECK_LOGIN = "CHECK_LOGIN", "检查登录态"
        OPEN_LOGIN = "OPEN_LOGIN", "打开登录页"
        BUILD_FRAME = "BUILD_FRAME", "建立当前游戏 Frame"
        KEEPALIVE = "KEEPALIVE", "保活当前游戏 Frame"

    class LoginState(models.TextChoices):
        READY = "READY", "已就绪"
        SCAN_REQUIRED = "SCAN_REQUIRED", "需要扫码"
        UNAVAILABLE = "UNAVAILABLE", "Chrome 不可用"
        UNKNOWN = "UNKNOWN", "待检查"

    class ErrorCategory(models.TextChoices):
        NONE = "NONE", "无"
        SCAN_REQUIRED = "SCAN_REQUIRED", "需要扫码"
        CHROME_UNAVAILABLE = "CHROME_UNAVAILABLE", "Chrome 不可用"
        OUTER_SESSION_UNREADY = "OUTER_SESSION_UNREADY", "外层会话未就绪"
        FRAME_UNREADY = "FRAME_UNREADY", "Frame 未就绪"
        TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE", "受控页面不可用"
        SOURCE_VERIFICATION_FAILED = "SOURCE_VERIFICATION_FAILED", "来源验证失败"
        GAME_NOT_SELECTABLE = "GAME_NOT_SELECTABLE", "当前账号无法切换目标游戏"
        SERVICE_STOPPING = "SERVICE_STOPPING", "服务正在停止"

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    login_state = models.CharField(max_length=20, choices=LoginState.choices, default=LoginState.UNKNOWN)
    outer_state = models.CharField(max_length=24, choices=OuterState.choices, default=OuterState.UNKNOWN)
    frame_state = models.CharField(max_length=16, choices=FrameState.choices, default=FrameState.UNKNOWN)
    available_actions = models.JSONField(default=list)
    login_wait_until = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    last_ready_at = models.DateTimeField(null=True, blank=True)
    error_category = models.CharField(
        max_length=32, choices=ErrorCategory.choices, default=ErrorCategory.NONE
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="game_circle_login_state_one")]
        verbose_name = "游戏圈登录运行态"
        verbose_name_plural = "游戏圈登录运行态"


class CandidatePost(models.Model):
    topic_id = models.BigIntegerField(unique=True)
    content_id = models.CharField(max_length=255, blank=True)
    origin_type = models.CharField(
        max_length=32, choices=OriginType.choices, default=OriginType.CLASSIFICATION_PENDING
    )
    classification_error = models.CharField(max_length=500, blank=True)
    classified_at = models.DateTimeField(null=True, blank=True)
    source_appid = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "候选帖子"
        verbose_name_plural = "候选帖子"


class OriginalPost(models.Model):
    topic_id = models.BigIntegerField(unique=True)
    content_id = models.CharField(max_length=255, unique=True)
    title = models.CharField(max_length=500, blank=True)
    content_html = models.TextField(blank=True)
    content_text = models.TextField(blank=True)
    ballot_text = models.TextField(blank=True)
    publish_time = models.DateTimeField(null=True, blank=True)
    status = models.IntegerField(null=True, blank=True)
    recommend_status = models.IntegerField(null=True, blank=True)
    source_appid = models.CharField(max_length=128, blank=True)
    author_openid = models.CharField(max_length=255, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "原创帖子"
        verbose_name_plural = "原创帖子"


class AuthorMuteStatus(models.Model):
    """Latest observed author mute state within one managed-game context."""

    openid = models.CharField(max_length=255)
    source_appid = models.CharField(max_length=128)
    nick_name = models.CharField(max_length=255, blank=True)
    local_muted_until = models.DateTimeField(null=True, blank=True)
    global_muted_until = models.DateTimeField(null=True, blank=True)
    first_detected_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField()
    source_requested_at = models.DateTimeField()
    last_source_post = models.ForeignKey(
        OriginalPost,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="author_mute_observations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("openid", "source_appid"), name="author_mute_scope")
        ]
        indexes = [models.Index(fields=("source_appid", "local_muted_until"))]
        verbose_name = "作者禁言状态"
        verbose_name_plural = "作者禁言状态"


class PostMedia(models.Model):
    post = models.ForeignKey(OriginalPost, on_delete=models.CASCADE, related_name="media")
    url = models.URLField(max_length=1000)
    media_type = models.CharField(max_length=16, choices=MediaType.choices, default=MediaType.IMAGE)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    is_gif = models.BooleanField(default=False)

    class Meta:
        verbose_name = "帖子媒体"
        verbose_name_plural = "帖子媒体"


class InteractionSnapshot(models.Model):
    post = models.ForeignKey(
        OriginalPost, on_delete=models.CASCADE, related_name="interaction_snapshots"
    )
    captured_at = models.DateTimeField(auto_now_add=True)
    view_count = models.PositiveBigIntegerField(null=True, blank=True)
    reply_count = models.PositiveBigIntegerField(default=0)
    love_count = models.PositiveBigIntegerField(default=0)
    share_count = models.PositiveBigIntegerField(default=0)
    source = models.CharField(max_length=64)

    class Meta:
        verbose_name = "互动快照"
        verbose_name_plural = "互动快照"
