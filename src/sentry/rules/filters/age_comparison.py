from __future__ import annotations

import operator
from datetime import datetime, timedelta
from typing import Sequence, Tuple

from django import forms
from django.utils import timezone

from sentry.eventstore.models import GroupEvent
from sentry.models import Group
from sentry.rules import EventState
from sentry.rules.filters.base import EventFilter
from sentry.types.condition_activity import ConditionActivity


class AgeComparisonType:
    OLDER = "older"
    NEWER = "newer"


timeranges = {
    "minute": ("minute(s)", timedelta(minutes=1)),
    "hour": ("hour(s)", timedelta(hours=1)),
    "day": ("day(s)", timedelta(days=1)),
    "week": ("week(s)", timedelta(days=7)),
}

age_comparison_choices = [(AgeComparisonType.OLDER, "older"), (AgeComparisonType.NEWER, "newer")]

age_comparison_map = {AgeComparisonType.OLDER: operator.lt, AgeComparisonType.NEWER: operator.gt}


def get_timerange_choices() -> Sequence[Tuple[str, str]]:
    return [
        (key, label)
        for key, (label, duration) in sorted(
            timeranges.items(), key=lambda key___label__duration: key___label__duration[1][1]
        )
    ]


class AgeComparisonForm(forms.Form):  # type: ignore
    comparison_type = forms.ChoiceField(choices=age_comparison_choices)
    value = forms.IntegerField()
    time = forms.ChoiceField(choices=get_timerange_choices)


class AgeComparisonFilter(EventFilter):
    id = "sentry.rules.filters.age_comparison.AgeComparisonFilter"
    form_cls = AgeComparisonForm
    form_fields = {
        "comparison_type": {"type": "choice", "choices": age_comparison_choices},
        "value": {"type": "number", "placeholder": 10},
        "time": {"type": "choice", "choices": get_timerange_choices()},
    }

    # An issue is newer/older than X minutes/hours/days/weeks
    label = "The issue is {comparison_type} than {value} {time}"
    prompt = "The issue is older or newer than..."

    def _validate(self, comparison_type: str | None, time: str, value: str) -> int:
        try:
            casted_value = int(value)
        except (TypeError, ValueError):
            return False

        if (
            not comparison_type
            or not time
            or time not in timeranges
            or (
                comparison_type != AgeComparisonType.OLDER
                and comparison_type != AgeComparisonType.NEWER
            )
        ):
            return False
        return casted_value

    def _compare(
        self, first_seen: datetime, comparison_type: str | None, time: str, value: int
    ) -> bool:
        _, delta_time = timeranges[time]
        if comparison_type is None:
            return False
        # type cast to bool to satisfy mypy
        return bool(
            age_comparison_map[comparison_type](first_seen + (value * delta_time), timezone.now())
        )

    def passes(self, event: GroupEvent, state: EventState) -> bool:
        comparison_type = self.get_option("comparison_type")
        time = self.get_option("time")
        value = self.get_option("value")

        validated_value = self._validate(comparison_type, time, value)
        if validated_value is False:
            return False

        return self._compare(event.group.first_seen, comparison_type, time, validated_value)

    def passes_activity(self, condition_activity: ConditionActivity) -> bool:
        comparison_type = self.get_option("comparison_type")
        time = self.get_option("time")
        value = self.get_option("value")

        validated_value = self._validate(comparison_type, time, value)
        if validated_value is False:
            return False

        try:
            group = Group.objects.get_from_cache(id=condition_activity.group_id)
        except Group.DoesNotExist:
            return False

        return self._compare(group.first_seen, comparison_type, time, validated_value)
