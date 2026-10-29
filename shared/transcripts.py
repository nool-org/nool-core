"""Speech-to-text guards shared by every gpt-4o-*-transcribe caller."""

import re


def echoes_prompt(transcript: str, prompt: str) -> bool:
    """gpt-4o-*-transcribe is generative: on silence or unclear audio it can return its own
    prompt as the transcript. True when most of the transcript's words come from the prompt."""
    t = set(re.findall(r"\w+", (transcript or "").lower()))
    p = set(re.findall(r"\w+", (prompt or "").lower()))
    return bool(t) and len(t & p) >= 0.6 * len(t)
