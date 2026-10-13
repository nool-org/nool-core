"""What each subject uses. Maths isn't tested by voice - spoken maths is
slow to say and hard to grade - so its teachers set written tests
(quiz_tests.py) and its students get the topic quiz's maths mix
(topic_quiz.QUIZ_MIX)."""

NO_VOICE_TESTS = {"Mathematics"}


def voice_tests_allowed(subject_name: str | None) -> bool:
    return (subject_name or "") not in NO_VOICE_TESTS
