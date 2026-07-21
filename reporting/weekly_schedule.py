"""Deterministic release schedule for weekly-operation metric publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


@dataclass(frozen=True)
class WeeklyReleasePhase:
    key: str
    weekday: int
    release_time: time


@dataclass(frozen=True)
class WeeklyReleasePlan:
    business_week_start: date
    phase: WeeklyReleasePhase


class WeeklyReleaseSchedule:
    """Maps Shanghai-local scheduler time to idempotent publication plans."""

    phases = (
        WeeklyReleasePhase("initial", 0, time(17, 30)),
        WeeklyReleasePhase("morning-recheck", 1, time(10, 0)),
        WeeklyReleasePhase("afternoon-recheck", 1, time(16, 0)),
    )

    @classmethod
    def due_plans(cls, local_now: datetime) -> tuple[WeeklyReleasePlan, ...]:
        weekday = local_now.weekday()
        if weekday > 1:
            return ()
        business_week_start = local_now.date() - timedelta(days=weekday + 7)
        now_time = local_now.time().replace(tzinfo=None)
        return tuple(
            WeeklyReleasePlan(business_week_start, phase)
            for phase in cls.phases
            if weekday > phase.weekday
            or (weekday == phase.weekday and now_time >= phase.release_time)
        )
