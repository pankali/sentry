from __future__ import annotations

__all__ = ["FeatureHandler", "BatchFeatureHandler", "ActiveReleaseDefaultOnHardcodeFeatureHandler"]

import abc
from typing import TYPE_CHECKING, Mapping, MutableSet, Optional, Sequence

if TYPE_CHECKING:
    from sentry.features.base import Feature
    from sentry.features.manager import FeatureCheckBatch
    from sentry.models import Organization, Project, User


class FeatureHandler:
    features: MutableSet[str] = set()

    def __call__(self, feature: Feature, actor: User) -> Optional[bool]:
        if feature.name not in self.features:
            return None

        return self.has(feature, actor)

    @abc.abstractmethod
    def has(self, feature: Feature, actor: User, skip_entity: Optional[bool] = False) -> bool:
        raise NotImplementedError

    def has_for_batch(self, batch: FeatureCheckBatch) -> Mapping[Project, bool]:
        # If not overridden, iterate over objects in the batch individually.
        return {
            obj: self.has(feature, batch.actor)
            for (obj, feature) in batch.get_feature_objects().items()
        }

    @abc.abstractmethod
    def batch_has(
        self,
        feature_names: Sequence[str],
        actor: User,
        projects: Optional[Sequence[Project]] = None,
        organization: Optional[Organization] = None,
        batch: bool = True,
    ) -> Optional[Mapping[str, Mapping[str, bool]]]:
        raise NotImplementedError


# It is generally better to extend BatchFeatureHandler if it is possible to do
# the check with no more than the feature name, organization, and actor. If it
# needs to unpack the Feature object and examine the flagged entity, extend
# FeatureHandler directly.


class BatchFeatureHandler(FeatureHandler):
    @abc.abstractmethod
    def _check_for_batch(self, feature_name: str, entity: Organization | User, actor: User) -> bool:
        raise NotImplementedError

    def has(self, feature: Feature, actor: User, skip_entity: Optional[bool] = False) -> bool:
        return self._check_for_batch(feature.name, feature.get_subject(), actor)

    def has_for_batch(self, batch: FeatureCheckBatch) -> Mapping[Project, bool]:
        flag = self._check_for_batch(batch.feature_name, batch.subject, batch.actor)
        return {obj: flag for obj in batch.objects}


# this project feature flag is on a hot path, but we still want to feature test it to certain projects
# before release
# instead of defining this as a flagr flag, we register this hard-coded FeatureHandler for performance
class ActiveReleaseDefaultOnHardcodeFeatureHandler(FeatureHandler):
    features = {"projects:active-release-monitor-default-on"}

    def has(self, feature: Feature, actor: User, skip_entity: Optional[bool] = False) -> bool:
        from sentry.features.base import ProjectFeature

        return (
            isinstance(feature, ProjectFeature)
            and feature.name in self.features
            and feature.project.id in [1, 11276, 5995946]
        )

    def batch_has(
        self,
        feature_names: Sequence[str],
        actor: User,
        projects: Optional[Sequence[Project]] = None,
        organization: Optional[Organization] = None,
        batch: bool = True,
    ) -> Optional[Mapping[str, Mapping[str, bool]]]:
        raise NotImplementedError
