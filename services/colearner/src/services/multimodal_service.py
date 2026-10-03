import json
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from google import genai
from google.genai import types

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

# Fixed model-specific data for the Multimodal pipeline (direct audio-in,
# no STT step) - no longer client-configurable, see
# src/domain/schemas.py's SessionConfig docstring for why. Values match
# nool-research's prior per-request defaults exactly.
MODEL_PROVIDER = "gemini"
LLM_MODEL = "gemini-3.5-flash-lite"
TTS_VOICE = "en-IN-NeerjaNeural"
TTS_RATE = "+0%"

_settings = get_settings()
gemini_client = genai.Client(api_key=_settings.gemini_api_key) if _settings.gemini_api_key else None



def _fallback_context(config: SessionConfig) -> str:
    """No textbook excerpt for this test - stay on the real chapter rather
    than drifting to an unrelated built-in topic."""
    return (
        f"No textbook excerpt was provided. Use only standard {config.grade} {config.subject} "
        f"syllabus content for the chapter '{config.chapter_name}' - never any other chapter."
    )


def build_multimodal_system_prompt(config: SessionConfig) -> str:
    """Builds direct audio multimodal system prompt with creative Bloom's taxonomy scenario questions."""
    context_text = config.textbook_context.strip() if config.textbook_context else _fallback_context(config)
    ref_list = config.reference_questions or []
    ref_questions_str = "\n".join(f"- {q}" for q in ref_list)
    blooms_str = ", ".join(config.blooms_attributes or ["Understanding", "Applying", "Analyzing"])

    return f"""You are an enthusiastic, curious, and sharp "Co-Learner & Study Companion" exploring {config.subject} ({config.chapter_name}) with your classmate.
You listen directly to the student's spoken audio. Listen attentively to their explanation, vocal confidence, and scientific accuracy.

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

OUTPUT FORMAT:
On every interactive turn, format your response in this EXACT two-line format:
[STUDENT]: <Transcribe what the student said in their audio clip verbatim>
[CO-LEARNER]: <Your concise, natural 2-3 sentence spoken reply balancing study-partner warmth with evaluator rigor>

WORKFLOW & TURN PROGRESSION:
- Turn 0 (Greeting): You have already greeted the classmate and asked: "Are you ready to begin?".
- Turn 1 (Readiness Confirmation): When the classmate confirms readiness (e.g. "Yes", "I'm ready", "Let's do it"), warmly acknowledge their readiness in one brief phrase, then ask QUESTION 1 as the open "what do you already know" starting question from rule 4 above - not a scenario puzzle.
- Turn 2 (Opening reframe, then the real ladder begins): After they answer Question 1, give the ONE gentle reframe from rule 4, then present CREATIVE QUESTION 2 matching the first target Bloom's level - this is where the graded scenario questions actually start.
- Subsequent Question Turns: For each student response, acknowledge their reasoning in 1-2 concise sentences per rule 3, and seamlessly introduce the NEXT creative scenario question matching the next target Bloom's level until exactly {config.num_questions} questions have been explored (Question 1's open starting question counts toward this total).
- Concluding Turn: Once Question {config.num_questions} is answered, provide a warm, honest 1-2 sentence peer summary of their strong points and what to review, then conclude in [CO-LEARNER]: "That concludes all questions for our session. Great job discussing these concepts today! [SESSION_COMPLETED]"
"""


def generate_multimodal_bloom_report(session: SessionState, full_transcript: str) -> Dict[str, Any]:
    """Generates Bloom's Taxonomy Cognitive Mapper Report for direct multimodal assessment."""
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
        resp = gemini_client.models.generate_content(
            model=LLM_MODEL,
            contents=mapping_prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        report_data = json.loads(resp.text)

        # Record tokens consumed by evaluation LLM call
        eval_in_toks = count_tokens(mapping_prompt, LLM_MODEL)
        eval_out_toks = count_tokens(json.dumps(report_data), LLM_MODEL)
        eval_metrics = calculate_turn_cost(LLM_MODEL, eval_in_toks, eval_out_toks, stt_seconds=0.0, tts_chars=0)
        record_turn_metrics(session, eval_metrics)
    except Exception as ex:
        print(f"Multimodal report generation error: {ex}")
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

    # Save to reports/
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_dir = get_settings().reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = reports_dir / f"multimodal_transcript_{session.session_id[:8]}_{timestamp}.txt"
    report_path = reports_dir / f"multimodal_report_{session.session_id[:8]}_{timestamp}.json"

    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(full_transcript)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    return report_data


async def process_multimodal_turn(
    session: SessionState,
    user_audio_bytes: Optional[bytes] = None,
    user_text: Optional[str] = None,
    time_limit_reached: bool = False,
    is_hint_request: bool = False,
) -> Tuple[Optional[str], str, str, bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """
    Processes a turn in the Pure Multimodal Direct Audio pipeline (Gemini
    native audio-in -> Edge-TTS). Accepts either raw audio bytes (from
    microphone) or direct user text (convenient for Swagger testing).
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
        system_prompt = build_multimodal_system_prompt(config)

        if gemini_client:
            session.gemini_chat = gemini_client.chats.create(
                model=LLM_MODEL,
                config=types.GenerateContentConfig(system_instruction=system_prompt)
            )

        session.turns.append({"student": "", "spoken": greeting_text})
        session.turn_index = 1

        # Track Turn 0 metrics
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

        transcript_lines = []
        for t in session.turns:
            if t.get("student"):
                transcript_lines.append(f"[STUDENT]: {t['student']}")
            if t.get("spoken"):
                transcript_lines.append(f"[CO-LEARNER]: {t['spoken']}")
        full_transcript = "\n".join(transcript_lines)

        report_data = generate_multimodal_bloom_report(session, full_transcript)
        session.report = report_data
        cost_report_data = generate_cost_report(session, "Multimodal", MODEL_PROVIDER, LLM_MODEL, TTS_VOICE)
        session.cost_report = cost_report_data

        # Timeout info is shown in UI notification pane only - not spoken out by model
        return None, "", "", True, report_data, None, cost_report_data, False

    # -------------------------------------------------------------------------
    # Hint Request (guiding nudge only - never an answer, never validates
    # correctness, never advances to the next question) - see
    # chained_service.py's identical branch and
    # src/api/routes/ai_assessor.py's is_hint_reply handling.
    # -------------------------------------------------------------------------
    if is_hint_request:
        hint_instruction = (
            "[The student is stuck and asked for a hint on the CURRENT question - this is "
            "not their answer, do not treat it as one. Give ONE short, thought-provoking "
            "nudge toward their own reasoning - a guiding question or a pointer to a "
            "relevant idea. Do NOT reveal, confirm, or state whether any answer is right "
            "or wrong, and do NOT move on to a new question - stay on this one and invite "
            "them to try again.]"
        )
        hint_text = "Let's think about that a bit more - what do you already know that might apply here?"
        in_toks = 0
        out_toks = 0
        if session.gemini_chat:
            resp = session.gemini_chat.send_message(hint_instruction)
            hint_text = resp.text.strip()
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                in_toks = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
                out_toks = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
        # Deliberately NOT appended to session.turns: a hint isn't a graded
        # Q&A exchange, and must not reach generate_multimodal_bloom_report's
        # transcript or count toward all_questions_answered.
        if not in_toks:
            in_toks = count_tokens(hint_instruction, LLM_MODEL)
        if not out_toks:
            out_toks = count_tokens(hint_text, LLM_MODEL)
        turn_metrics = calculate_turn_cost(LLM_MODEL, in_toks, out_toks, stt_seconds=0.0, tts_chars=len(hint_text))
        record_turn_metrics(session, turn_metrics)
        audio_b64 = await synthesize_speech_base64(hint_text, voice=TTS_VOICE, rate=TTS_RATE)
        return None, hint_text, audio_b64, False, None, turn_metrics, None, True

    # -------------------------------------------------------------------------
    # Interactive Turn (Direct Audio or Text Fallback for Testing)
    # -------------------------------------------------------------------------
    raw_ai_output = ""
    student_transcript = ""
    co_learner_reply = ""
    audio_seconds = estimate_audio_duration_seconds(user_audio_bytes) if user_audio_bytes else 0.0
    in_toks = 0
    out_toks = 0

    if not user_audio_bytes and user_text:
        student_transcript = user_text.strip()
        if session.gemini_chat:
            resp = session.gemini_chat.send_message(
                f"[STUDENT]: {student_transcript}\nReply in [STUDENT] and [CO-LEARNER] format:"
            )
            raw_ai_output = resp.text.strip()
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                in_toks = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
                out_toks = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
    elif not user_audio_bytes:
        co_learner_reply = "I didn't hear your response. Could you please answer again?"
    elif session.gemini_chat:
        _extension, mime = detect_audio_container(user_audio_bytes)
        audio_part = types.Part.from_bytes(data=user_audio_bytes, mime_type=mime)
        response = session.gemini_chat.send_message(
            message=[
                audio_part,
                "Listen to the student's spoken audio above and reply in the [STUDENT] and [CO-LEARNER] format:"
            ]
        )
        raw_ai_output = response.text.strip()
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            in_toks = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
            out_toks = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

    # Parse [STUDENT] and [CO-LEARNER] delimiters
    if "[STUDENT]:" in raw_ai_output and "[CO-LEARNER]:" in raw_ai_output:
        parts = raw_ai_output.split("[CO-LEARNER]:")
        parsed_student = parts[0].replace("[STUDENT]:", "").strip()
        if parsed_student:
            student_transcript = parsed_student
        co_learner_reply = parts[1].strip() if len(parts) > 1 else raw_ai_output
    elif "[STUDENT]:" in raw_ai_output:
        lines = raw_ai_output.split("\n")
        parsed_student = lines[0].replace("[STUDENT]:", "").strip()
        if parsed_student:
            student_transcript = parsed_student
        co_learner_reply = "\n".join(lines[1:]).strip()
    elif raw_ai_output:
        co_learner_reply = raw_ai_output
        if not student_transcript:
            student_transcript = "(Direct audio answer heard)"

    if not co_learner_reply:
        co_learner_reply = "I hear your answer. Let's look closer at that concept."

    if not in_toks:
        in_toks = count_tokens(student_transcript, LLM_MODEL) + int(audio_seconds * 32)
    if not out_toks:
        out_toks = count_tokens(co_learner_reply, LLM_MODEL)

    # Calculate turn cost metrics
    turn_metrics = calculate_turn_cost(LLM_MODEL, in_toks, out_toks, stt_seconds=0.0, tts_chars=len(co_learner_reply))
    record_turn_metrics(session, turn_metrics)

    # Check for completion and time limit
    is_completed = False
    report_data = None
    cost_report_data = None
    clean_ai_reply = co_learner_reply

    elapsed_seconds = (datetime.now() - session.created_at).total_seconds()
    time_limit_reached = elapsed_seconds >= config.session_max_time_seconds

    # Calculate user turns including the current turn
    total_user_turns = sum(1 for t in session.turns if t.get("student")) + (1 if student_transcript else 0)
    # Turn 1 is readiness ack ("Ready"), turns 2..N+1 are answers to the N questions
    all_questions_answered = total_user_turns >= (config.num_questions + 1)

    if "[SESSION_COMPLETED]" in co_learner_reply or "that concludes our session" in co_learner_reply.lower() or time_limit_reached or all_questions_answered:
        is_completed = True
        if all_questions_answered and "that concludes our session" not in clean_ai_reply.lower():
            clean_ai_reply += "\nThat concludes all questions for our session. Great job discussing these concepts today!"
        session.is_completed = True

    # MUST append current turn to session.turns BEFORE building full_transcript
    session.turns.append({"student": student_transcript, "spoken": clean_ai_reply})
    session.turn_index += 1

    if is_completed:
        transcript_lines = []
        for t in session.turns:
            if t.get("student"):
                transcript_lines.append(f"[STUDENT]: {t['student']}")
            if t.get("spoken"):
                transcript_lines.append(f"[CO-LEARNER]: {t['spoken']}")
        full_transcript = "\n".join(transcript_lines)

        report_data = generate_multimodal_bloom_report(session, full_transcript)
        session.report = report_data

        cost_report_data = generate_cost_report(session, "Multimodal", MODEL_PROVIDER, LLM_MODEL, TTS_VOICE)
        session.cost_report = cost_report_data

    audio_b64 = await synthesize_speech_base64(clean_ai_reply, voice=TTS_VOICE, rate=TTS_RATE)
    return student_transcript, clean_ai_reply, audio_b64, is_completed, report_data, turn_metrics, cost_report_data, False
