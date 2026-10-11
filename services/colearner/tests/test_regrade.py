from src.api.routes import regrade

_BODY = {
    "config": {"grade": "Grade 10", "subject": "Science", "chapter_name": "Electricity", "questions": [
        {"id": "q1", "order": 1, "bloom_level": "REMEMBER", "spoken_text": "What is Ohm's law?",
         "expected_points": ["V = IR"], "misconceptions": []}]},
    "transcript": [{"role": "assistant", "text": "What is Ohm's law?", "question_number": 1},
                   {"role": "user", "content": "V equals I R", "question_number": 1}],
    "answers": [{"question_number": 1, "hints_used": 2}],
}


def test_regrade_requires_auth(client):
    assert client.post("/api/v1/colearner/regrade", json=_BODY).status_code == 401


def test_regrade_grades_the_stored_transcript_with_the_rubric(client, monkeypatch):
    monkeypatch.setattr("src.api.deps.verify_token", lambda token: object())
    seen = {}

    async def fake_grade(session, transcript, rubric=None):
        seen.update(transcript=transcript, rubric=rubric, chapter=session.config.chapter_name)
        return {"qa_transcripts": [{"question_number": 1, "score_percent": 80}], "session_summary": {}}

    monkeypatch.setattr(regrade, "grade_transcript", fake_grade)
    resp = client.post("/api/v1/colearner/regrade", json=_BODY, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200
    assert resp.json()["qa_transcripts"][0]["score_percent"] == 80
    assert seen["chapter"] == "Electricity"
    assert "[Q1] [STUDENT]: V equals I R" in seen["transcript"]
    assert "[Q1] [CO-LEARNER]: What is Ohm's law?" in seen["transcript"]
    assert seen["rubric"] == [{"question_number": 1, "bloom_level": "REMEMBER", "question": "What is Ohm's law?",
                               "key_points": ["V = IR"], "misconceptions": [], "hints_used": 2,
                               "version": "standard"}]


def test_regrade_uses_the_version_asked(client, monkeypatch):
    monkeypatch.setattr("src.api.deps.verify_token", lambda token: object())
    seen = {}

    async def fake_grade(session, transcript, rubric=None):
        seen.update(rubric=rubric)
        return {"qa_transcripts": [], "session_summary": {}}

    monkeypatch.setattr(regrade, "grade_transcript", fake_grade)
    body = {**_BODY, "answers": [{"question_number": 1, "hints_used": 0, "version": "easier"}]}
    body["config"] = {**_BODY["config"], "questions": [{**_BODY["config"]["questions"][0], "easier": {
        "spoken_text": "What does V = IR mean?", "expected_points": ["voltage equals current times resistance"]}}]}
    assert client.post("/api/v1/colearner/regrade", json=body, headers={"Authorization": "Bearer t"}).status_code == 200
    assert seen["rubric"][0]["version"] == "easier" and seen["rubric"][0]["question"] == "What does V = IR mean?"
    assert seen["rubric"][0]["key_points"] == ["voltage equals current times resistance"]
