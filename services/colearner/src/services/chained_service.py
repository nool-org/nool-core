import io
import json
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from openai import AsyncOpenAI

from src.config.settings import get_settings
from src.domain.schemas import SessionConfig
from src.services.audio_service import detect_audio_container, synthesize_speech_base64
from src.services.cost_service import (
    calculate_turn_cost,
    count_tokens,
    estimate_audio_duration_seconds,
    generate_cost_report,
    record_turn_metrics,
)
from src.services.session_store import SessionState

# Fixed model-specific data for the Chained pipeline (Whisper STT -> OpenAI
# chat LLM -> Edge-TTS). No longer client-configurable - see
# src/domain/schemas.py's SessionConfig docstring for why. Values match
# nool-research's prior per-request defaults exactly.
MODEL_PROVIDER = "openai"
STT_MODEL = "whisper-1"
LLM_MODEL = "gpt-4o-mini"
TTS_VOICE = "en-IN-NeerjaNeural"
TTS_RATE = "+0%"

_settings = get_settings()
openai_client = AsyncOpenAI(api_key=_settings.openai_api_key) if _settings.openai_api_key else None



def _fallback_context(config: SessionConfig) -> str:
    """No textbook excerpt for this test - stay on the real chapter rather
    than drifting to an unrelated built-in topic."""
    return (
        f"No textbook excerpt was provided. Use only standard {config.grade} {config.subject} "
        f"syllabus content for the chapter '{config.chapter_name}' - never any other chapter."
    )


def build_chained_system_prompt(config: SessionConfig) -> str:
    """Builds the Co-Learner system prompt with creative Bloom's taxonomy scenario questions."""
    context_text = config.textbook_context.strip() if config.textbook_context else _fallback_context(config)
    ref_list = config.reference_questions or []
    ref_questions_str = "\n".join(f"- {q}" for q in ref_list)
    blooms_str = ", ".join(config.blooms_attributes or ["Understanding", "Applying", "Analyzing"])

    return f"""You are an enthusiastic, curious, and sharp "Co-Learner & Study Companion" exploring {config.subject} ({config.chapter_name}) with your classmate.

CORE SYLLABUS & TEXTBOOK CONTEXT:
\"\"\"
{context_text}
\"\"\"

REFERENCE TOPICS & CONCEPTS (Inspirational Reference Material):
\"\"\"
{ref_questions_str}
\"\"\"

SESSION TARGETS:
- Grade Level: {config.grade}
- Total Questions to Explore: {config.num_questions}
- Target Bloom's Taxonomy Cognitive Levels: {blooms_str}

YOUR ROLE AS AN AUTHENTIC CO-LEARNER:
1. THE CURIOUS STUDY BUDDY: You sound like an engaged peer learning together, NOT an intimidating examiner or robotic questionnaire. You bring concepts to life through creative, real-world scenario thought experiments (e.g. household gadgets, appliances, vehicles, weather, sports, design puzzles).
2. SIMPLE, FOCUSED QUESTIONS (DO NOT JUST REPEAT THE REFERENCE QUESTIONS):
   - Use the reference topics and textbook context as your conceptual foundation.
   - Do NOT simply recite the sample reference questions word-for-word.
   - This is an assessment, not a lesson: each question tests ONE concept in plain, direct language the student can fully take in on a single listen - no multi-part setups, no stacked "what if" parameters, no long wind-up.
   - If you use a real-world scenario, keep the setup to one short sentence - it's a light hook, not a puzzle to untangle.
   - Match your questions to test the target Bloom's Taxonomy levels ({blooms_str}):
     * "Understanding": Ask them to explain a concept or everyday observation in their own words.
     * "Applying": Change ONE condition (not several at once) and ask what happens.
     * "Analyzing": Ask them to compare two things or explain a cause-and-effect - one clear question, not a layered mystery.
3. NEVER VALIDATE, ONLY ACKNOWLEDGE: You never say or imply whether an answer is right, wrong, correct, or incorrect - not even a hint of judgment - and you never give false praise either. After every answer FROM QUESTION 2 ONWARD, give a brief, warm, neutral acknowledgment that you heard their reasoning (e.g. "Got it, thanks for walking me through that," "Interesting way to look at it") without confirming, correcting, or grading it, then move straight to the next question. You are gathering how they think, not scoring them turn by turn - that judgment happens later, not in the conversation. You also never volunteer hints, examples, or explanations unprompted - guidance only ever comes as a separate, explicitly-requested nudge (handled outside this normal question flow), and even then it never reveals or confirms the answer.
4. START BY LISTENING, NOT TESTING: Question 1 is always different from every question after it - it is never a scenario puzzle. Instead, ask a genuine, open, in-your-own-words starting question (e.g. "So, before we dive in - what do you already know about {config.chapter_name}?"). You are trying to find out how they already think about this, not testing them yet. Once they answer, this is the ONE moment in the whole session where you go beyond a neutral acknowledgment: offer ONE brief, warm sentence that gently helps them visualize or reframe what they just described in your own words - a simple analogy or a clearer way to picture the idea - without ever telling them they were right or wrong, and without turning it into a lecture (one sentence, not a mini-lesson - you're a curious classmate riffing on their idea, not a teacher correcting it). Then move straight into Question 2, which starts the real Bloom's-level ladder. Rule 3's plain neutral acknowledgment applies to every question from here on - the gentle reframe is reserved for this opening moment only, since every question after it needs to stay a clean, ungrounded data point for later scoring.

SPOKEN VOICE & SPEECH GUIDELINES:
- Voice Pacing: Keep your responses concise and crisp (1 to 2 sentences maximum). School students listen better to short, lively spoken turns.
- Conversational Transitions: Speak like a real classmate (e.g., "Oh, that's a neat way to put it!", "Good point about the current, but what about...", "Here's another puzzle I was wondering about...").
- Spoken Math: Never speak raw LaTeX ($...), symbols (rho, ohm, Delta), or dry formula code aloud. Translate equations into clear, natural spoken language (e.g., "resistance depends on length and cross-sectional area" or "heat produced depends on the square of current").
- Spoken Chemistry: Always read chemical formulas and equations the way a teacher says them aloud, never as raw notation. Read subscripts as spoken numbers ("H2O" -> "H two O", or just say "water"), name compounds when that's clearer ("CaCO3" -> "calcium carbonate"), read charges as "positive"/"negative" or "plus"/"minus" (never "superscript"), and read reaction arrows as "yields", "produces", or "reacts to form" (e.g. "2H2 + O2 -> 2H2O" as "two hydrogen plus one oxygen yields two water") - never say "arrow" or spell out the raw symbols.

WORKFLOW & TURN PROGRESSION:
- Turn 0 (Greeting): You have already greeted the classmate and asked: "Are you ready to begin?".
- Turn 1 (Readiness Confirmation): When the classmate confirms readiness (e.g. "Yes", "I'm ready", "Let's do it"), warmly acknowledge their readiness in one brief phrase, then ask QUESTION 1 as the open "what do you already know" starting question from rule 4 above - not a scenario puzzle.
- Turn 2 (Opening reframe, then the real ladder begins): After they answer Question 1, give the ONE gentle reframe from rule 4, then present CREATIVE QUESTION 2 matching the first target Bloom's level - this is where the graded scenario questions actually start.
- Subsequent Question Turns: For each student response, acknowledge their reasoning in 1-2 concise sentences per rule 3, and seamlessly introduce the NEXT creative scenario question matching the next target Bloom's level until exactly {config.num_questions} questions have been explored (Question 1's open starting question counts toward this total).
- Concluding Turn: Once Question {config.num_questions} is answered, provide a warm, honest 1-2 sentence peer summary of their strong points and what to review, then conclude with: "That concludes all questions for our session. Great job discussing these concepts today! [SESSION_COMPLETED]"
"""


async def transcribe_audio_bytes(wav_bytes: bytes, hint: str = "") -> str:
    """Transcribes audio bytes using OpenAI Whisper. `hint` (subject/chapter)
    biases Whisper toward the session's vocabulary - science terms like
    "calcium oxide" otherwise come back misheard."""
    if not wav_bytes or not openai_client:
        return ""
    try:
        extension, _mime = detect_audio_container(wav_bytes)
        audio_file = io.BytesIO(wav_bytes)
        audio_file.name = f"student_speech{extension}"
        transcription = await openai_client.audio.transcriptions.create(
            model=STT_MODEL,
            file=audio_file,
            language="en",
            prompt=hint,
        )
        return transcription.text.strip()
    except Exception as e:
        print(f"Whisper STT Error: {e}")
        return ""


async def generate_bloom_report(session: SessionState, full_transcript: str) -> Dict[str, Any]:
    """Generates structured Bloom's Taxonomy Cognitive Mapper Report adhering to notebook schema."""
    config = session.config

    mapping_prompt = f"""Based on the following complete oral assessment transcript between the Co-Learner and the student on '{config.chapter_name}', generate a structured JSON evaluation report adhering strictly to this schema:

```json
{{
  "session_summary": {{
    "grade": "{config.grade}",
    "subject": "{config.subject}",
    "chapter": "{config.chapter_name}",
    "total_questions_asked": {config.num_questions},
    "overall_feedback": "<1-2 sentences summarizing student engagement and conceptual understanding>",
    "overall_mastery_percent": <0-100 integer, the average of every qa_transcripts entry's score_percent>
  }},
  "qa_transcripts": [
    {{
      "question_number": 1,
      "blooms_attribute": "<Understanding / Applying / Analyzing / Remembering / Evaluating / Creating>",
      "question_asked": "<Exact question asked by Co-Learner>",
      "student_response": "<Student response summary or spoken answer>",
      "probing_or_hints_given": "<Any hints/probes given or null>",
      "score_percent": <0-100 integer, how well the response demonstrates mastery of this question's target Bloom's level and the underlying concept - not just "did they answer">,
      "is_substantively_correct": <true/false, a stricter pass/fail read of the same response>
    }}
  ]
}}
```

CRITICAL INSTRUCTIONS:
- Include all substantive questions asked and the student's corresponding responses in 'qa_transcripts'.
- Do NOT include the introductory readiness exchange ("Are you ready to begin?") as an assessment question.
- Accurately map each question and answer to its target Bloom's Taxonomy level (Remembering, Understanding, Applying, Analyzing, Evaluating, Creating).
- Grade each response on its merits: a confident but wrong or vague answer scores low; a correct, well-reasoned answer scores high. Be an honest evaluator, not a cheerleader.

TRANSCRIPT:
\"\"\"
{full_transcript}
\"\"\"
"""
    try:
        resp = await openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert Bloom's Taxonomy educational mapper. Output strictly valid JSON."},
                {"role": "user", "content": mapping_prompt}
            ],
            response_format={"type": "json_object"}
        )
        report_data = json.loads(resp.choices[0].message.content)

        # Record tokens consumed by evaluation LLM call
        eval_in_toks = count_tokens(mapping_prompt, LLM_MODEL)
        eval_out_toks = count_tokens(json.dumps(report_data), LLM_MODEL)
        eval_metrics = calculate_turn_cost(LLM_MODEL, eval_in_toks, eval_out_toks, stt_seconds=0.0, tts_chars=0)
        record_turn_metrics(session, eval_metrics)
    except Exception as ex:
        print(f"Report generation error: {ex}")
        report_data = {
            "session_summary": {
                "grade": config.grade,
                "subject": config.subject,
                "chapter": config.chapter_name,
                "total_questions_asked": config.num_questions,
                "overall_feedback": "Session concluded successfully."
            },
            "qa_transcripts": []
        }

    # Save transcript and report files to reports/
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_dir = get_settings().reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = reports_dir / f"chained_transcript_{session.session_id[:8]}_{timestamp}.txt"
    report_path = reports_dir / f"chained_report_{session.session_id[:8]}_{timestamp}.json"

    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(full_transcript)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    return report_data


async def process_chained_turn(
    session: SessionState,
    user_audio_bytes: Optional[bytes] = None,
    user_text: Optional[str] = None,
    time_limit_reached: bool = False,
    is_hint_request: bool = False,
) -> Tuple[Optional[str], str, str, bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """
    Processes a turn in the Chained pipeline (Whisper STT -> gpt-4o-mini -> Edge-TTS).
    Accepts either raw audio bytes (from microphone) or direct user text (convenient for Swagger testing).
    Returns: (student_transcript, co_learner_text, co_learner_audio_base64, is_completed, report, turn_metrics, cost_report, is_hint_reply)
    """
    config = session.config

    # -------------------------------------------------------------------------
    # Turn 0: Opening Greeting
    # -------------------------------------------------------------------------
    if session.turn_index == 0 and not user_audio_bytes and not user_text and not time_limit_reached:
        greeting_text = (
            f"Hi! Welcome to our {config.subject} discussion on {config.chapter_name}. "
            f"We'll explore {config.num_questions} quick real-world scenario{'s' if config.num_questions > 1 else ''} together. "
            f"Are you ready to begin?"
        )
        system_prompt = build_chained_system_prompt(config)

        session.openai_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "assistant", "content": greeting_text}
        ]
        session.turns.append({"role": "assistant", "content": greeting_text})
        session.turn_index = 1

        # Track initial turn metrics
        in_toks = count_tokens(system_prompt, LLM_MODEL)
        out_toks = count_tokens(greeting_text, LLM_MODEL)
        turn_metrics = calculate_turn_cost(LLM_MODEL, in_toks, out_toks, stt_seconds=0.0, tts_chars=len(greeting_text))
        record_turn_metrics(session, turn_metrics)

        audio_b64 = await synthesize_speech_base64(greeting_text, voice=TTS_VOICE, rate=TTS_RATE)
        return None, greeting_text, audio_b64, False, None, turn_metrics, None, False

    # -------------------------------------------------------------------------
    # Timeout Wrap-up (Handled strictly in backend - NO fake student turn created)
    # -------------------------------------------------------------------------
    elapsed_seconds = (datetime.now() - session.created_at).total_seconds()
    server_time_limit_reached = elapsed_seconds >= config.session_max_time_seconds
    is_timeout_wrapup = time_limit_reached or (server_time_limit_reached and not user_audio_bytes and not user_text)

    if is_timeout_wrapup:
        session.is_completed = True

        full_transcript = "\n".join(
            f"[{('CO-LEARNER' if t.get('role') == 'assistant' else 'STUDENT')}]: {t.get('content', '')}"
            for t in session.turns if t.get('content')
        )
        report_data = await generate_bloom_report(session, full_transcript)
        session.report = report_data
        cost_report_data = generate_cost_report(session, "Chained", MODEL_PROVIDER, LLM_MODEL, TTS_VOICE)
        session.cost_report = cost_report_data

        # Timeout info is shown in UI notification pane only - not spoken out by model
        return None, "", "", True, report_data, None, cost_report_data, False

    # -------------------------------------------------------------------------
    # Hint Request (guiding nudge only - never an answer, never validates
    # correctness, never advances to the next question) - see
    # src/api/routes/ai_assessor.py's is_hint_reply branch for how the
    # bridge keeps the student on the same question afterward.
    # -------------------------------------------------------------------------
    if is_hint_request:
        session.openai_messages.append({
            "role": "user",
            "content": (
                "[The student is stuck and asked for a hint on the CURRENT question - "
                "this is not their answer, do not treat it as one. Give ONE short, "
                "thought-provoking nudge toward their own reasoning - a guiding question "
                "or a pointer to a relevant idea. Do NOT reveal, confirm, or state whether "
                "any answer is right or wrong, and do NOT move on to a new question - stay "
                "on this one and invite them to try again.]"
            ),
        })
        resp = await openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=session.openai_messages,
            temperature=0.5,
        )
        hint_text = resp.choices[0].message.content.strip()
        session.openai_messages.append({"role": "assistant", "content": hint_text})
        # Deliberately NOT appended to session.turns: a hint isn't a graded
        # Q&A exchange, and must not reach generate_bloom_report's transcript
        # or count toward all_questions_answered.

        if hasattr(resp, "usage") and resp.usage:
            in_toks = resp.usage.prompt_tokens
            out_toks = resp.usage.completion_tokens
        else:
            in_toks = sum(count_tokens(m.get("content", ""), LLM_MODEL) for m in session.openai_messages)
            out_toks = count_tokens(hint_text, LLM_MODEL)
        turn_metrics = calculate_turn_cost(LLM_MODEL, in_toks, out_toks, stt_seconds=0.0, tts_chars=len(hint_text))
        record_turn_metrics(session, turn_metrics)

        audio_b64 = await synthesize_speech_base64(hint_text, voice=TTS_VOICE, rate=TTS_RATE)
        return None, hint_text, audio_b64, False, None, turn_metrics, None, True

    # -------------------------------------------------------------------------
    # Interactive User Turn
    # -------------------------------------------------------------------------
    student_transcript = ""
    stt_seconds = 0.0
    if user_audio_bytes:
        stt_seconds = estimate_audio_duration_seconds(user_audio_bytes)
        student_transcript = await transcribe_audio_bytes(
            user_audio_bytes, hint=f"A student answering a {config.subject} question about {config.chapter_name}."
        )
    elif user_text:
        student_transcript = user_text.strip()

    if not student_transcript:
        student_transcript = "(Inaudible audio response)"

    session.turns.append({"role": "user", "content": student_transcript})

    # Query text reasoning LLM
    session.openai_messages.append({"role": "user", "content": student_transcript})
    resp = await openai_client.chat.completions.create(
        model=LLM_MODEL,
        messages=session.openai_messages,
        temperature=0.5
    )
    ai_raw_reply = resp.choices[0].message.content.strip()
    if hasattr(resp, "usage") and resp.usage:
        in_toks = resp.usage.prompt_tokens
        out_toks = resp.usage.completion_tokens
    else:
        in_toks = sum(count_tokens(m.get("content", ""), LLM_MODEL) for m in session.openai_messages)
        out_toks = count_tokens(ai_raw_reply, LLM_MODEL)
    session.openai_messages.append({"role": "assistant", "content": ai_raw_reply})

    turn_metrics = calculate_turn_cost(LLM_MODEL, in_toks, out_toks, stt_seconds=stt_seconds, tts_chars=len(ai_raw_reply))
    record_turn_metrics(session, turn_metrics)

    # Check for session completion or time limit
    is_completed = False
    report_data = None
    cost_report_data = None
    clean_ai_reply = ai_raw_reply

    elapsed_seconds = (datetime.now() - session.created_at).total_seconds()
    time_limit_reached = elapsed_seconds >= config.session_max_time_seconds
    user_turns_count = sum(1 for t in session.turns if t.get("role") == "user")
    # Turn 1 is readiness ack ("Ready"), turns 2..N+1 are answers to the N questions
    all_questions_answered = user_turns_count >= (config.num_questions + 1)

    if "[SESSION_COMPLETED]" in ai_raw_reply or "that concludes our session" in ai_raw_reply.lower() or time_limit_reached or all_questions_answered:
        is_completed = True
        clean_ai_reply = ai_raw_reply.replace("[SESSION_COMPLETED]", "").strip()
        if all_questions_answered and "that concludes our session" not in clean_ai_reply.lower():
            clean_ai_reply += "\nThat concludes all questions for our session. Great job discussing these concepts today!"
        session.is_completed = True

    # MUST append assistant turn to session.turns BEFORE building full_transcript
    session.turns.append({"role": "assistant", "content": clean_ai_reply})
    session.turn_index += 1

    if is_completed:
        # Format complete transcript with ALL turns included
        full_transcript = "\n".join(
            f"[{('CO-LEARNER' if t.get('role') == 'assistant' else 'STUDENT')}]: {t.get('content', '')}"
            for t in session.turns if t.get('content')
        )
        report_data = await generate_bloom_report(session, full_transcript)
        session.report = report_data

        # Generate and save separate Cost Analysis report
        cost_report_data = generate_cost_report(session, "Chained", MODEL_PROVIDER, LLM_MODEL, TTS_VOICE)
        session.cost_report = cost_report_data

    audio_b64 = await synthesize_speech_base64(clean_ai_reply, voice=TTS_VOICE, rate=TTS_RATE)
    return student_transcript, clean_ai_reply, audio_b64, is_completed, report_data, turn_metrics, cost_report_data, False
