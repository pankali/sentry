from datetime import timedelta

from django.utils import timezone
from freezegun import freeze_time

from sentry.models import Activity, Group
from sentry.rules.history.preview import PREVIEW_TIME_RANGE, preview
from sentry.testutils import TestCase
from sentry.testutils.silo import region_silo_test
from sentry.types.activity import ActivityType


def get_hours(time: timedelta) -> int:
    return time.days * 24 + time.seconds // (60 * 60)


@freeze_time()
@region_silo_test
class ProjectRulePreviewTest(TestCase):
    def _set_up_first_seen(self):
        hours = get_hours(PREVIEW_TIME_RANGE)
        for i in range(hours):
            for j in range(i % 5):
                Group.objects.create(
                    project=self.project, first_seen=timezone.now() - timedelta(hours=i + 1)
                )
        return hours

    def _set_up_activity(self, condition_type):
        hours = get_hours(PREVIEW_TIME_RANGE)
        for i in range(hours):
            group = Group.objects.create(id=i, project=self.project)
            Activity.objects.create(
                project=self.project,
                group=group,
                type=condition_type.value,
                datetime=timezone.now() - timedelta(hours=i + 1),
            )
        return hours

    def _test_preview(self, condition, result1, result2):
        conditions = [{"id": condition}]
        result = preview(self.project, conditions, [], "all", "all", 0)
        assert result.count() == result1

        result = preview(self.project, conditions, [], "all", "all", 120)
        assert result.count() == result2

    def test_first_seen(self):
        hours = self._set_up_first_seen()
        self._test_preview(
            "sentry.rules.conditions.first_seen_event.FirstSeenEventCondition",
            (hours - 1) * 2,
            (hours - 1) * 2 / 5,
        )

    def test_regression(self):
        hours = self._set_up_activity(ActivityType.SET_REGRESSION)
        self._test_preview(
            "sentry.rules.conditions.regression_event.RegressionEventCondition",
            hours,
            hours / 2,
        )

    def test_reappeared(self):
        hours = self._set_up_activity(ActivityType.SET_UNRESOLVED)
        self._test_preview(
            "sentry.rules.conditions.reappeared_event.ReappearedEventCondition", hours, hours / 2
        )

    def test_age_comparison(self):
        hours = get_hours(PREVIEW_TIME_RANGE)
        conditions = [{"id": "sentry.rules.conditions.first_seen_event.FirstSeenEventCondition"}]
        threshold = 24
        filters = [
            {
                "id": "sentry.rules.filters.age_comparison.AgeComparisonFilter",
                "comparison_type": "newer",
                "time": "hour",
                "value": threshold,
            }
        ]
        groups = []
        for i in range(hours):
            groups.append(
                Group.objects.create(
                    id=i, project=self.project, first_seen=timezone.now() - timedelta(hours=i + 1)
                )
            )

        result = preview(self.project, conditions, filters, "all", "all", 0)
        # this filter is strictly older/newer
        for i in range(threshold - 1):
            assert groups[i] in result
        for i in range(threshold - 1, hours):
            assert groups[i] not in result

    def test_occurrences(self):
        hours = get_hours(PREVIEW_TIME_RANGE)
        groups = []
        for i in range(hours):
            groups.append(
                Group.objects.create(
                    project=self.project,
                    first_seen=timezone.now() - timedelta(hours=i + 1),
                    times_seen=i,
                )
            )
        # regression events to trigger conditions
        for group in groups:
            Activity.objects.create(
                project=self.project,
                group=group,
                type=ActivityType.SET_REGRESSION.value,
                datetime=timezone.now() - timedelta(hours=1),
            )
        conditions = [{"id": "sentry.rules.conditions.regression_event.RegressionEventCondition"}]
        threshold = 24
        filters = [
            {
                "id": "sentry.rules.filters.issue_occurrences.IssueOccurrencesFilter",
                "value": threshold,  # issue has occurred at least 24 times
            }
        ]

        result = preview(self.project, conditions, filters, "all", "all", 0)
        for i in range(threshold + 1):
            assert groups[i] not in result
        for i in range(threshold + 1, hours):
            assert groups[i] in result

    def test_unsupported_conditions(self):
        self._set_up_first_seen()
        # conditions with no immediate plan to support
        unsupported_conditions = [
            "sentry.rules.conditions.tagged_event.TaggedEventCondition",
            "sentry.rules.conditions.event_frequency.EventFrequencyCondition",
            "sentry.rules.conditions.event_frequency.EventFrequencyPercentCondition",
            "sentry.rules.conditions.event_attribute.EventAttributeCondition",
            "sentry.rules.conditions.level.LevelCondition",
        ]
        for condition in unsupported_conditions:
            result = preview(self.project, [{"id": condition}], [], "all", "all", 60)
            assert result is None

        # empty condition
        assert None is preview(self.project, [], [], "all", "all", 60)

    def test_mutually_exclusive_conditions(self):
        mutually_exclusive = [
            {"id": "sentry.rules.conditions.first_seen_event.FirstSeenEventCondition"},
            {"id": "sentry.rules.conditions.regression_event.RegressionEventCondition"},
            {"id": "sentry.rules.conditions.regression_event.ReappearedEventCondition"},
        ]

        result = preview(self.project, mutually_exclusive, [], "all", "all", 60)
        assert len(result) == 0
