"""Small deterministic portion of the game-circle session refresh state machine."""

from __future__ import annotations

from enum import StrEnum
class RefreshMode(StrEnum):
    RECOVER = "RECOVER"
    KEEPALIVE = "KEEPALIVE"


class SourceIdentityResult(StrEnum):
    ACCEPTED = "ACCEPTED"
    TOKEN_REJECTED = "TOKEN_REJECTED"
    BUSINESS_ERROR = "BUSINESS_ERROR"


def classify_source_identity_result(errcode: object) -> SourceIdentityResult:
    """Only the observed query rejection code may invalidate an identity lease."""
    if errcode == 0:
        return SourceIdentityResult.ACCEPTED
    if errcode == -2:
        return SourceIdentityResult.TOKEN_REJECTED
    return SourceIdentityResult.BUSINESS_ERROR


class RefreshDirectFailure(RuntimeError):
    """A refresh cannot proceed until an operator restores the external session."""


def validate_refresh_preconditions(*, scan_required: bool, has_outer_token: bool) -> None:
    """Classify only the explicit outer-session boundaries."""
    if scan_required or not has_outer_token:
        raise RefreshDirectFailure("需要人工扫码")
