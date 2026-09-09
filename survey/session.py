import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from survey.questions import (
    QUESTIONS, ROUTING, TERMINAL_STATES, SYS_AUDIO,
    get_next_question_id, Question
)
from survey.answer_parser import ParsedAnswer


SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TurnRecord:
    question_id: str
    audio_played: str           # filename served to caller
    twi_transcript: str         # raw ASR output
    english_translation: str    # MT output
    answer_key: str             # parsed routing key
    extracted_value: Any        # entity or None
    confidence: float
    parser_used: str
    reprompt: bool              # True if this was a re-prompt attempt
    asr_time: float
    mt_time: float
    llm_time: float
    timestamp: float            # unix time


@dataclass
class Session:
    session_id: str
    farmer_phone: str
    start_time: float
    turns: list[TurnRecord] = field(default_factory=list)
    current_question_id: str = "OPEN"
    reprompt_counts: dict[str, int] = field(default_factory=dict)
    is_complete: bool = False
    completion_reason: str = ""    # "completed" | "callback" | "timeout" | "error"
    post_call_analysis: dict = field(default_factory=dict)   # Q8 LLM themes

    # ------------------------------------------------------------------
    # Computed helpers
    # ------------------------------------------------------------------

    def elapsed(self) -> float:
        return time.time() - self.start_time

    def get_answer(self, question_id: str) -> Optional[TurnRecord]:
        for t in reversed(self.turns):
            if t.question_id == question_id and not t.reprompt:
                return t
        return None

    def current_question(self) -> Question:
        return QUESTIONS[self.current_question_id]

    # ------------------------------------------------------------------
    # Core advance logic
    # ------------------------------------------------------------------

    def advance(self, parsed: ParsedAnswer,
                twi_transcript: str = "",
                asr_time: float = 0.0,
                mt_time: float = 0.0) -> dict:
        """
        Process a parsed answer and advance session state.

        Returns a dict:
          {
            "action":       "reprompt" | "advance" | "complete",
            "next_question_id": str,
            "next_audio":   str,   # filename to play next
            "answer_logged": ParsedAnswer,
          }
        """
        q = self.current_question()
        is_unclear = parsed.answer_key == "unclear"
        reprompt_count = self.reprompt_counts.get(q.id, 0)

        # ---- Record the turn ----
        turn = TurnRecord(
            question_id=q.id,
            audio_played=q.audio_file,
            twi_transcript=twi_transcript,
            english_translation=parsed.raw_text,
            answer_key=parsed.answer_key,
            extracted_value=parsed.extracted_value,
            confidence=parsed.confidence,
            parser_used=parsed.parser_used,
            reprompt=False,
            asr_time=asr_time,
            mt_time=mt_time,
            llm_time=parsed.llm_time,
            timestamp=time.time(),
        )
        self.turns.append(turn)

        # ---- Re-prompt logic ----
        if is_unclear and q.allows_reprompt and reprompt_count == 0:
            self.reprompt_counts[q.id] = 1
            turn.reprompt = True
            self._save()
            return {
                "action": "reprompt",
                "next_question_id": q.id,
                "next_audio": q.reprompt_audio,
                "answer_logged": parsed,
            }

        # If still unclear after re-prompt, use the question's fallback route
        effective_key = parsed.answer_key
        if is_unclear and q.unclear_fallback:
            # Override with the defined fallback — routing table picks it up via "unclear" key
            effective_key = "unclear"

        # ---- Advance to next question ----
        next_id = get_next_question_id(q.id, effective_key, self.elapsed())

        if next_id in TERMINAL_STATES:
            self.is_complete = True
            self.completion_reason = "completed" if next_id == "CLOSE" else "callback"
            self.current_question_id = next_id
            self._save()
            return {
                "action": "complete",
                "next_question_id": next_id,
                "next_audio": SYS_AUDIO.get(next_id, SYS_AUDIO["CLOSE"]),
                "answer_logged": parsed,
            }

        self.current_question_id = next_id
        self._save()
        return {
            "action": "advance",
            "next_question_id": next_id,
            "next_audio": QUESTIONS[next_id].audio_file,
            "answer_logged": parsed,
        }

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["elapsed_seconds"] = round(self.elapsed(), 2)
        return d

    def _save(self) -> None:
        path = SESSIONS_DIR / f"{self.session_id}.json"
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)

    @classmethod
    def load(cls, session_id: str) -> "Session":
        path = SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Session {session_id} not found")
        with open(path) as f:
            data = json.load(f)
        # Reconstruct turns
        turns = [TurnRecord(**t) for t in data.pop("turns", [])]
        data.pop("elapsed_seconds", None)
        session = cls(**{k: v for k, v in data.items() if k != "turns"})
        session.turns = turns
        return session


# ---------------------------------------------------------------------------
# Session store — in-memory index backed by JSON files
# ---------------------------------------------------------------------------

class SessionStore:

    @classmethod
    def create(cls, farmer_phone: str = "") -> Session:
        session = Session(
            session_id=str(uuid.uuid4()),
            farmer_phone=farmer_phone,
            start_time=time.time(),
        )
        session._save()
        return session

    @classmethod
    def get(cls, session_id: str) -> Session:
        # Always load from disk — avoids stale state when running with multiple
        # gunicorn workers (each process has its own memory; disk is the shared truth).
        return Session.load(session_id)

    @classmethod
    def list_all(cls) -> list[dict]:
        records = []
        for path in sorted(SESSIONS_DIR.glob("*.json")):
            try:
                with open(path) as f:
                    d = json.load(f)
                records.append({
                    "session_id":       d["session_id"],
                    "farmer_phone":     d.get("farmer_phone", ""),
                    "start_time":       d["start_time"],
                    "current_question": d["current_question_id"],
                    "is_complete":      d["is_complete"],
                    "completion_reason":d.get("completion_reason", ""),
                    "turn_count":       len(d.get("turns", [])),
                    "elapsed_seconds":  d.get("elapsed_seconds", 0),
                })
            except Exception:
                pass
        return records
