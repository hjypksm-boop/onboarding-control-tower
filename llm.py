"""
Thin wrapper around the Groq API for the agent layer (default model: openai/gpt-oss-120b).

The LLM only handles language: it classifies edge cases and drafts recommended actions,
prioritisation and tone. Every figure, and every sentence that states or combines figures,
is written by code from the SQL views in metrics.sql. Guardrails:
- Number guard: drafted text may not contain any digits apart from record IDs such as CP-0024.
  If it does, the draft is rejected and the code-written fallback is used.
- Human review: every piece of LLM-written text is prefixed with DRAFT_LABEL.
- Template fallback: with no GROQ_API_KEY, or on any API error, the code-written fallback is used,
  so the app still runs offline.

GROQ_API_KEY (and optionally GROQ_MODEL) are read from .env, which is gitignored.
"""
import os
import re
import sys

import groq
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
# gpt-oss models reason before answering. Reasoning tokens count against the completion limit,
# so keep the effort low, leave headroom for it, and don't return the reasoning text.
REASONING = MODEL.startswith("openai/gpt-oss")
REASONING_HEADROOM = 1024

DRAFT_LABEL = "[DRAFT – review before sending]"
RECORD_ID = re.compile(r"\b(?:CP|CL|ST|TP)-\d+\b")  # IDs may appear in drafts; other digits may not

RULES = """
Rules:
- Do not write any numbers: no counts, amounts, percentages, durations or dates. The figures are already
  stated next to your text. Refer to stages by their ID (for example CP-0024) or client name.
- Use only the facts provided. Do not add facts, causes or dates that are not in them.
- Write plain text. Use "-" for bullets.
"""

_client = None


def client():
    """Groq client, or None when GROQ_API_KEY isn't set."""
    global _client
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    if _client is None:
        _client = groq.Groq(api_key=key, timeout=30, max_retries=2)
    return _client


def source_label():
    return f"LLM: {MODEL}" if client() else "template (set GROQ_API_KEY in .env to use the LLM)"


def figures(text):
    """Digits left in text once record IDs are removed. Drafts must have none."""
    return re.findall(r"\d[\d,.:%]*", RECORD_ID.sub("", text))


def _chat(system, user, max_tokens):
    extra = {"reasoning_effort": "low", "include_reasoning": False} if REASONING else {}
    resp = client().chat.completions.create(
        model=MODEL, temperature=0, max_completion_tokens=max_tokens + (REASONING_HEADROOM if REASONING else 0),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **extra)
    return (resp.choices[0].message.content or "").strip()


def draft(instructions, context, fallback, max_tokens=300):
    """LLM-drafted prose with no figures, prefixed with DRAFT_LABEL. Returns (text, source).

    context is the code-written facts the draft is about. fallback is code-written text used,
    unlabelled, when there's no key, the API fails, or the draft contains figures.
    """
    if client() is None:
        return fallback, "template"
    try:
        text = _chat(instructions.strip() + "\n" + RULES, "Facts:\n" + context, max_tokens)
    except groq.GroqError as e:
        print(f"[llm] {type(e).__name__}: {e} - using template", file=sys.stderr)
        return fallback, "template"
    text = re.sub("[‐‑‒]", "-", text)  # models sometimes write CP‑0024 with a Unicode hyphen
    found = figures(text)
    if not text or found:
        print(f"[llm] draft contained figures ({', '.join(found)}) - using template", file=sys.stderr)
        return fallback, "template"
    return f"{DRAFT_LABEL}\n{text}", "llm"


def classify(question, text, labels, fallback):
    """Pick one of labels for text. Returns (label, source); fallback if no key or no valid label."""
    if client() is None:
        return fallback, "template"
    prompt = f"{question}\nAllowed labels: {', '.join(labels)}\nText: {text}\nAnswer with one label only."
    try:
        answer = _chat("You classify short operational notes. Reply with a single label from the list.",
                       prompt, max_tokens=10)
    except groq.GroqError as e:
        print(f"[llm] {type(e).__name__}: {e} - using fallback label", file=sys.stderr)
        return fallback, "template"
    match = next((l for l in labels if answer.strip(" .\"'").lower() == l.lower()), None)
    return (match, "llm") if match else (fallback, "template")
