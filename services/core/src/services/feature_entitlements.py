from src.domain.models import Feature, Plan, School


def effective_enabled_features(*, plan: Plan | None, school: School | None = None) -> list[Feature] | None:
    """None = every feature enabled. The plan bundles modules (Plan.enabled_features,
    None = all); a Super Admin can switch some off per school (School.disabled_features).
    A school gets a module only when both allow it - see requirements.md §7.14.
    """
    off = set(school.disabled_features or []) if school is not None else set()
    if (plan is None or plan.enabled_features is None) and not off:
        return None
    bundled = list(Feature) if plan is None or plan.enabled_features is None else [
        Feature(value) for value in plan.enabled_features]
    return [f for f in bundled if f.value not in off]


def is_feature_enabled(feature: Feature, *, plan: Plan | None, school: School | None = None) -> bool:
    effective = effective_enabled_features(plan=plan, school=school)
    return effective is None or feature in effective
