# Grader accuracy gate

Checks that the Bloom grader (`src/services/evaluation`) tells right answers from wrong ones.
`grader_eval.py` grades every session in `grader_set.json` with the real grader `--repeats` times
(default 2) and prints:

- `verdict_accuracy_pct` - `is_substantively_correct` agrees with the label (a labelled answer the grader skipped counts as a miss)
- `wrong_marked_correct` / `right_marked_wrong` - false passes / false fails
- `mastery_gap_mean` / `_max` - |reported mastery - mean score of the matched answers| per session
- `cost_usd` - real grader cost of the run (about $0.09 for 2 repeats of the current set)

Exit code 1 (gate fails) if accuracy < 95% or more than 1 in 30 wrong answers are marked correct.
Run it after any change to the grader prompt, schema or model.

## Run locally

Inside the colearner image, with this checkout's code mounted (from the nool-core root, Git Bash):

```bash
OPENAI_API_KEY=... MSYS_NO_PATHCONV=1 docker run --rm -e OPENAI_API_KEY -e PYTHONPATH=/app \
  -v "$PWD/services/colearner/src:/app/src:ro" -v "$PWD/shared:/app/shared:ro" \
  -v "$PWD/services/colearner/evals:/app/evals:ro" -w /app nool-core-colearner \
  python evals/grader_eval.py --repeats 2
```

Or in a venv from `services/colearner` with `PYTHONPATH=.:../..`: `python evals/grader_eval.py`.
CI: Actions -> "Grader eval" -> Run workflow (manual only; uses the `OPENAI_API_KEY` secret).

The scoring arithmetic is unit-tested without the API in `tests/test_grader_eval_scoring.py`
(skipped when `evals/` is not next to `tests/`).

## The data set

`grader_set.json` -> `sessions[]`, each `{id, source, grade, subject, chapter, turns[], rubric?}`.
`turns` is the transcript in order, `{role: "co_learner"|"student", text}`; a student turn that is an
answer to grade also has `label`: `"correct"`, `"wrong"` or `"ambiguous"` (kept in the transcript,
excluded from scoring), plus an optional `note`. Unlabelled turns (readiness, small talk, hint
requests) are context only. `rubric` is passed straight to `grade_transcript(rubric=...)`.
Optional per turn: `question_number` and `kind` (`follow_up`, `repeat_request`, `noise`) - the transcript is
then written as production writes it (`[Q2] [STUDENT] (repeat request): ...`); `min_score`/`max_score` - the
answer's final score must fall in that band (else `score_band_misses`, gate fails); `no_answer: true` - the
grader may skip it (e.g. the transcription prompt echoed back). Any `student_response` with `[` or `]` fails
the gate; `score_spread_max` (one answer's max - min score across repeats) is reported only.

The `qa-*` sessions (AI-008) are the failure cases QA saw, with rubrics: mixed-language and Indian-English
answers, "I don't know" in Tamil, a prompt injection, a prompt echo, a rubric paraphrase, a follow-up that
completes an answer, hints counted from the rubric, an easier version (capped at 70), a confident
misconception, and Chemistry, Biology and Maths questions.

The current 9 sessions are synthetic: Electricity & Circuits sessions from the old realtime / chained /
multimodal pipelines, with gpt-4o-written student answers (strong = `correct`, weak = `wrong`) heard
through STT. Two opener answers are `ambiguous`: "electricity flows from negative to positive because
electrons are negative" and "electricity flows from positive to negative terminal". They were written as
wrong answers, but one is right for electron flow and the other is right for conventional current, so
the verdict depends on how loosely "electricity" is read, not on the grader's quality.

## Adding real student answers

1. Take a finished session's transcript (the realtime `/finish` response `transcript`, or the stored
   session turns) and copy it into a new `sessions[]` entry as `turns`, roles mapped to `co_learner`/`student`.
2. Have a teacher label each student answer turn: `correct` if the core idea is right for the grade's
   textbook, `wrong` otherwise, `ambiguous` (with a `note`) if teachers would disagree. For a multi-turn
   answer, label the turn the grader should judge (usually the first full answer) and leave the rest unlabelled.
3. Set `grade`/`subject`/`chapter`, and `source: "real: <school>/<date>"` (no student names or ids in the text).
4. If the test had prepped questions, add `rubric: [{question_number, key_points, misconceptions}]`.
5. Run the gate; keep wrong answers at 30+ in total so the 1-in-30 false-pass limit stays meaningful.

## Insights accuracy (AI-021)

`insights_eval.py` runs about 20 post-test fact sets (built from `grader_set.json`, plus a few hand-written
edge cases) through `insights_service` with each model (`--models`, default gpt-5-mini and gpt-4o-mini) and
counts the recaps that break the prompt's own rules: a number said in `recap_text`, a number in the headline or
next steps that is not in the facts, a next-step reference that names none of the facts' topics. It also
reports latency (production waits 15 s per call) and prints sample recaps for a tone review. No gate yet.
Same run recipe as the grader eval; the checkers are unit-tested in `tests/test_insights_eval.py`.
