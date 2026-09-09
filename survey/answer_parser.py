import re
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from dotenv import load_dotenv, dotenv_values
from pathlib import Path
import os

from survey.questions import Question, ParserType, Option

# Resolve .env relative to this file so it loads correctly regardless of cwd
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

# ---------------------------------------------------------------------------
# Claude via bearer token — single shared async client
# ---------------------------------------------------------------------------
# Set these in .env:
#   CLAUDE_API_URL  — full endpoint, e.g. https://bedrock-gateway.example.com/v1/messages
#   AWS_BEARER_TOKEN_BEDROCK  — bearer token
#   CLAUDE_MODEL    — model ID (default: claude-3-5-haiku-20241022)

_BEDROCK_BASE    = os.getenv("BEDROCK_BASE_URL", "https://bedrock-runtime.us-east-1.amazonaws.com")
_CLAUDE_MODEL    = os.getenv("CLAUDE_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")


def _invoke_url() -> str:
    return f"{_BEDROCK_BASE}/model/{os.getenv('CLAUDE_MODEL', _CLAUDE_MODEL)}/invoke"


async def _claude(prompt: str, max_tokens: int = 256) -> str:
    """
    Send a single-turn prompt to Claude Haiku 4.5 on Bedrock.
    Auth: Authorization: Bearer <AWS_BEARER_TOKEN_BEDROCK> — the only credential needed.
    Body uses Bedrock format: anthropic_version instead of model field.
    """
    # Re-read from .env on every call so token refreshes take effect without a restart
    env = dotenv_values(_ENV_PATH)
    token = env.get("AWS_BEARER_TOKEN_BEDROCK") or os.getenv("AWS_BEARER_TOKEN_BEDROCK", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(_invoke_url(), json=body, headers=headers)
        resp.raise_for_status()
    data = resp.json()
    return data["content"][0]["text"].strip()


# ---------------------------------------------------------------------------
# Thresholds & precompiled patterns
# ---------------------------------------------------------------------------

FUZZY_UNCLEAR_THRESHOLD = 0.15
_NEGATION_WINDOW = 25

# Compiled once — used by both keyword and fuzzy parsers
_NEGATION_RE = re.compile(
    r"\b("
    r"not|no|never"
    r"|don'?t|doesn'?t|didn'?t"
    r"|haven'?t|hasn'?t|hadn'?t"
    r"|won'?t|wouldn'?t|can'?t|cannot|couldn'?t|shouldn'?t"
    r"|isn'?t|aren'?t|wasn'?t|weren'?t"
    r"|did not|does not|do not|have not|has not|had not"
    r"|will not|would not|could not|should not"
    r")\b"
)


# ---------------------------------------------------------------------------
# ParsedAnswer — returned by every parser
# ---------------------------------------------------------------------------

@dataclass
class ParsedAnswer:
    question_id: str
    raw_text: str              # English translation from MT
    answer_key: str            # routing key ("yes" / "no" / "planted" / … / "unclear")
    extracted_value: Any       # entity dict for LLM/regex; None for keyword/fuzzy
    confidence: float          # 0.0 – 1.0
    parser_used: str           # "keyword" | "fuzzy" | "regex" | "llm" | "raw"
    llm_time: float = 0.0      # seconds spent on Claude call (0 if unused)


# ---------------------------------------------------------------------------
# Keyword parser — yes/no and simple binary questions
# ---------------------------------------------------------------------------

def _is_negated(text: str, kw_start: int) -> bool:
    """True if a negation word immediately precedes the keyword."""
    context = text[max(0, kw_start - _NEGATION_WINDOW): kw_start]
    return bool(_NEGATION_RE.search(context))


def _parse_keyword(text: str, question: Question) -> tuple[str, float]:
    normalised = re.sub(r"\b(i think|i believe|well|um|uh|hmm)\b", "", text.lower().strip())

    scores: dict[str, int] = {}
    for option in question.options:
        hits = 0
        for kw in option.keywords:
            # Word-boundary match prevents "no" from hitting inside "know"/"now"/"not"/etc.
            for m in re.finditer(rf'\b{re.escape(kw)}\b', normalised):
                if not _is_negated(normalised, m.start()):
                    hits += 1
        scores[option.key] = hits

    best_key = max(scores, key=lambda k: scores[k]) if scores else None
    best_hits = scores.get(best_key, 0) if best_key else 0

    if best_hits == 0:
        return "unclear", 0.0
    return best_key, min(0.5 + 0.1 * best_hits, 1.0)


# ---------------------------------------------------------------------------
# Fuzzy parser — multi-option closed questions
# ---------------------------------------------------------------------------

def _negated_tokens(text_lower: str) -> set:
    """Return word tokens that are preceded by a negation within _NEGATION_WINDOW chars."""
    result = set()
    for m in re.finditer(r'\b(\w+)\b', text_lower):
        if _is_negated(text_lower, m.start()):
            result.add(m.group())
    return result


def _score_option(text: str, option: Option, negated: Optional[set] = None) -> float:
    text_lower = text.lower()
    if negated is None:
        negated = set()
    # Substring keyword hits with negation check — substring keeps morphological matches
    # (e.g. "plan" hitting inside "planning") while negation prevents "not planted" → planted
    kw_hits = 0
    for kw in option.keywords:
        pos = text_lower.find(kw)
        while pos != -1:
            if not _is_negated(text_lower, pos):
                kw_hits += 1
            pos = text_lower.find(kw, pos + 1)
    kw_score = kw_hits / max(len(option.keywords), 1)
    # Token overlap, excluding tokens that are negated in the input
    option_tokens = set(re.findall(r'\w+', option.text.lower()))
    text_tokens   = set(re.findall(r'\w+', text_lower))
    effective_overlap = (option_tokens & text_tokens) - negated
    overlap = len(effective_overlap) / max(len(option_tokens), 1)
    return max(kw_score, overlap)


def _parse_fuzzy(text: str, question: Question) -> tuple[str, float]:
    if not question.options:
        return "unclear", 0.0
    text_lower = text.lower()
    negated = _negated_tokens(text_lower)
    scores = [(opt.key, _score_option(text, opt, negated)) for opt in question.options]
    best_key, best_score = max(scores, key=lambda x: x[1])
    if best_score < FUZZY_UNCLEAR_THRESHOLD:
        return "unclear", 0.0
    return best_key, round(best_score, 3)


# ---------------------------------------------------------------------------
# Regex parser — extract quantity (Q2A: bags / acres)
# ---------------------------------------------------------------------------

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "fifty": 50, "hundred": 100,
}


def _parse_regex_quantity(text: str) -> Optional[dict]:
    t = text.lower()
    result: dict[str, int] = {}
    bags_m  = re.search(r'(\d+)\s*(?:bags?|sacks?)', t)
    acres_m = re.search(r'(\d+)\s*acres?', t)
    if bags_m:
        result["bags"]  = int(bags_m.group(1))
    if acres_m:
        result["acres"] = int(acres_m.group(1))
    if not result:
        num_m = re.search(r'\b(\d+)\b', t)
        if num_m:
            result["quantity"] = int(num_m.group(1))
        else:
            for word, val in _WORD_NUMS.items():
                if re.search(rf'\b{word}\b', t):
                    result["quantity"] = val
                    break
    return result if result else None


# ---------------------------------------------------------------------------
# LLM parser — open-ended short answers (Q3A, Q5, Q6) via Claude
# ---------------------------------------------------------------------------

_LLM_PROMPTS: dict[str, str] = {
    "Q3A": (
        "A Ghanaian farmer was asked which maize seed they bought instead of BM270.\n"
        "Response: \"{text}\"\n\n"
        "Extract only the seed variety or brand name. "
        "Return ONLY valid JSON — no explanation, no markdown:\n"
        "{{\"seed_name\": \"<name>\"}} or {{\"seed_name\": null}}"
    ),
    "Q5": (
        "A Ghanaian farmer was asked which fertilizer they bought this season.\n"
        "Response: \"{text}\"\n\n"
        "Extract only the fertilizer name or brand. "
        "Return ONLY valid JSON — no explanation, no markdown:\n"
        "{{\"fertilizer_name\": \"<name>\"}} or {{\"fertilizer_name\": null}}"
    ),
    "Q6": (
        "A Ghanaian farmer was asked where they bought their fertilizer — "
        "which agro shop or dealer.\n"
        "Response: \"{text}\"\n\n"
        "Extract the shop name, dealer name, or location. "
        "Return ONLY valid JSON — no explanation, no markdown:\n"
        "{{\"dealer_name\": \"<name>\"}} or {{\"dealer_name\": null}}"
    ),
}


async def _parse_llm(text: str, question_id: str) -> tuple[Any, float]:
    """
    Call Claude to extract a named entity from an open-ended response.
    Returns (extracted_dict, llm_seconds), or (None, llm_seconds) on any error.
    """
    prompt_template = _LLM_PROMPTS.get(question_id)
    if not prompt_template:
        return None, 0.0

    t0 = time.time()
    try:
        raw = await _claude(prompt_template.format(text=text))
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        extracted = json.loads(raw.strip())
    except Exception:
        return None, time.time() - t0

    return extracted, time.time() - t0


# ---------------------------------------------------------------------------
# Post-call LLM — Q8 theme coding (run after call ends, not during)
# ---------------------------------------------------------------------------

async def code_q8_themes(raw_translation: str) -> dict:
    """
    Extract themes from the farmer's Q8 field-day recall answer.
    Deferred post-call to avoid adding latency during the live call.
    """
    prompt = (
        "A Ghanaian farmer was asked what they remember most from a Demeter Ghana field day.\n"
        f"Response: \"{raw_translation}\"\n\n"
        "Return ONLY valid JSON — no explanation, no markdown:\n"
        "{\"themes\": [\"...\"], "
        "\"product_mentions\": [\"...\"], "
        "\"sentiment\": \"positive|neutral|negative\", "
        "\"summary\": \"<one sentence>\"}"
    )
    t0 = time.time()
    try:
        raw = await _claude(prompt, max_tokens=512)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
    except Exception as e:
        result = {"error": str(e), "raw": raw_translation}

    result["llm_time"] = round(time.time() - t0, 4)
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def parse_answer(question: Question, english_text: str) -> ParsedAnswer:
    """
    Parse the farmer's English-translated response for the given question.
    Returns a ParsedAnswer with the routing key and any extracted value.
    """
    text = english_text.strip()

    # DTMF / digit input — "1" → first option, "2" → second, etc.
    # Handles phone keypad presses and Playground text input.
    if text.isdigit() and question.options:
        idx = int(text) - 1
        if 0 <= idx < len(question.options):
            option = question.options[idx]
            return ParsedAnswer(question.id, text, option.key, None, 1.0, "dtmf")

    if question.parser_type == ParserType.KEYWORD:
        key, conf = _parse_keyword(text, question)
        return ParsedAnswer(question.id, text, key, None, conf, "keyword")

    elif question.parser_type == ParserType.FUZZY:
        key, conf = _parse_fuzzy(text, question)
        return ParsedAnswer(question.id, text, key, None, conf, "fuzzy")

    elif question.parser_type == ParserType.REGEX:
        extracted = _parse_regex_quantity(text)
        key  = "value" if extracted else "unclear"
        conf = 0.9     if extracted else 0.0
        return ParsedAnswer(question.id, text, key, extracted, conf, "regex")

    elif question.parser_type == ParserType.LLM:
        extracted, llm_time = await _parse_llm(text, question.id)
        if extracted is None:
            # LLM call failed or no prompt defined — treat as unclear, advance via * wildcard
            return ParsedAnswer(question.id, text, "unclear", None, 0.0, "llm", llm_time)
        has_value = any(v is not None for v in extracted.values()) if isinstance(extracted, dict) else bool(extracted)
        key  = "value" if has_value else "unclear"
        conf = 0.85    if has_value else 0.0
        return ParsedAnswer(question.id, text, key, extracted, conf, "llm", llm_time)

    else:  # RAW — Q8, stored verbatim and coded post-call
        return ParsedAnswer(question.id, text, "raw", {"raw": text}, 1.0, "raw")
