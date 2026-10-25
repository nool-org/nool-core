from src.domain.models import Feature

from .common import CamelModel


class FeaturesOut(CamelModel):
    """A school's feature entitlements - see School.enabled_features'
    docstring. None = no override set, every feature is enabled.
    """

    enabled_features: list[Feature] | None


class UpdateFeaturesIn(CamelModel):
    enabled_features: list[Feature] | None


class SchoolFeaturesOut(CamelModel):
    """A school's modules: what its plan bundles (None = all), what a Super Admin
    switched off for it, and what it gets (both allow it; None = all)."""

    plan_features: list[Feature] | None
    disabled_features: list[Feature]
    enabled_features: list[Feature] | None


class UpdateSchoolFeaturesIn(CamelModel):
    disabled_features: list[Feature]
