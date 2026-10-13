"""Which ways of checking learning each subject uses - one place, enforced by
the server (voice tests, written tests, the topic quiz) and sent to the app
(SubjectOut.formats) so it only offers these.

- voice: Co-learner voice tests and spoken answers. Not Maths - spoken maths
  is slow to say and hard to grade.
- mcq, who_is_right, fix_steps, make_your_own: written question types
  (question_types.py). make_your_own's code checks are number theory, so
  Maths only; Maths is problem-first, so it gets all three."""

FORMATS: dict[str, tuple[str, ...]] = {
    "Mathematics": ("mcq", "who_is_right", "fix_steps", "make_your_own"),
}
DEFAULT_FORMATS = ("mcq", "voice")


def formats(subject_name: str | None) -> tuple[str, ...]:
    return FORMATS.get(subject_name or "", DEFAULT_FORMATS)


def voice_tests_allowed(subject_name: str | None) -> bool:
    return "voice" in formats(subject_name)
