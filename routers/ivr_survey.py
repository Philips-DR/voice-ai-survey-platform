import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from survey.answer_parser import parse_answer, code_q8_themes
from survey.questions import QUESTIONS, SYS_AUDIO
from survey.session import Session, SessionStore

router = APIRouter()

AUDIOS_DIR = Path("audios")
AUDIO_BASE = "/survey/audio"
# URL of the deployed ayaspeech-models-api — set in .env
ASR_MT_API_URL = os.getenv("ASR_MT_API_URL", "http://localhost:8001")
ASR_MT_ENDPOINT = f"{ASR_MT_API_URL}/transcribe_and_translate/"


def _audio_url(filename: Optional[str]) -> Optional[str]:
    return f"{AUDIO_BASE}/{filename}" if filename else None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class StartRequest(BaseModel):
    farmer_phone: str = ""


class TextRespondRequest(BaseModel):
    """For testing routing without real audio — skip ASR/MT, supply text directly."""
    session_id: str
    english_text: str    # pre-translated response (dev/test mode)


class PostCallRequest(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _call_asr_mt(audio_path: str) -> tuple[str, str, float, float]:
    """
    POST audio to the ayaspeech ASR+MT API.
    Returns (twi_transcript, english_translation, asr_time, mt_time).
    Raises HTTPException on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            with open(audio_path, "rb") as f:
                files = {"audio_file": (Path(audio_path).name, f, "audio/wav")}
                resp = await client.post(ASR_MT_ENDPOINT, files=files)
        resp.raise_for_status()
        data = resp.json()
        return (
            data.get("transcription") or "",
            data.get("translation") or "",
            data.get("asr_inference_time") or 0.0,
            data.get("translation_inference_time") or 0.0,
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"ASR/MT API error: {e.response.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"ASR/MT API unreachable: {e}")


def _session_state(session: Session) -> dict:
    """Compact view of current session state for API responses."""
    q = QUESTIONS.get(session.current_question_id)
    audio_file = q.audio_file if q else SYS_AUDIO.get(session.current_question_id)
    return {
        "session_id":            session.session_id,
        "current_question_id":   session.current_question_id,
        "current_question_text": q.text if q else None,
        "current_parser_type":   q.parser_type.value if q else None,
        "current_options":       [{"key": o.key, "text": o.text} for o in q.options] if q else [],
        "next_audio":            audio_file,
        "audio_url":             _audio_url(audio_file),
        "elapsed_seconds":       round(session.elapsed(), 2),
        "turn_count":            len(session.turns),
        "is_complete":           session.is_complete,
        "completion_reason":     session.completion_reason,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/start",
    summary="Start a new survey call session",
    description="Creates a session and returns the opening audio to play to the farmer.")
async def start_survey(body: StartRequest):
    session = SessionStore.create(farmer_phone=body.farmer_phone)
    first_q = QUESTIONS["OPEN"]
    return JSONResponse({
        "session_id":            session.session_id,
        "current_question_id":   "OPEN",
        "current_question_text": first_q.text,
        "current_parser_type":   first_q.parser_type.value,
        "current_options":       [{"key": o.key, "text": o.text} for o in first_q.options],
        "next_audio":            first_q.audio_file,
        "audio_url":             _audio_url(first_q.audio_file),
        "elapsed_seconds":       0.0,
    })


@router.post("/respond/audio",
    summary="Submit farmer audio response",
    description=(
        "Accepts a WAV/MP3 audio file of the farmer's spoken response. "
        "Runs ASR → MT → answer parser → routing logic. "
        "Returns the next question audio to play."
    ))
async def respond_with_audio(
    session_id: str = Form(...),
    audio_file: UploadFile = File(...),
):
    try:
        session = SessionStore.get(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.is_complete:
        raise HTTPException(status_code=400, detail="Session already complete")

    # Save to temp file
    suffix = os.path.splitext(audio_file.filename or ".wav")[1] or ".wav"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(audio_file.file, tmp)
            tmp_path = tmp.name

        twi_transcript, english_translation, asr_time, mt_time = await _call_asr_mt(tmp_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return await _process_response(
        session, english_translation, twi_transcript, asr_time, mt_time
    )


@router.post("/respond/text",
    summary="Submit pre-translated text response (dev/test mode)",
    description=(
        "Skips ASR and MT. Accepts English text directly. "
        "Use this to test routing logic without live models."
    ))
async def respond_with_text(body: TextRespondRequest):
    try:
        session = SessionStore.get(body.session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.is_complete:
        raise HTTPException(status_code=400, detail="Session already complete")

    return await _process_response(
        session,
        english_text=body.english_text,
        twi_transcript="[text mode — no ASR]",
        asr_time=0.0,
        mt_time=0.0,
    )


async def _process_response(
    session: Session,
    english_text: str,
    twi_transcript: str,
    asr_time: float,
    mt_time: float,
) -> JSONResponse:
    """Shared logic for both audio and text respond endpoints."""
    question = session.current_question()
    parsed = await parse_answer(question, english_text)

    result = session.advance(
        parsed,
        twi_transcript=twi_transcript,
        asr_time=asr_time,
        mt_time=mt_time,
    )

    response = {
        **_session_state(session),
        "question_answered": question.id,
        "answer_key":        parsed.answer_key,
        "extracted_value":   parsed.extracted_value,
        "confidence":        parsed.confidence,
        "parser_used":       parsed.parser_used,
        "action":            result["action"],
        "inference_times": {
            "asr_seconds": round(asr_time, 4),
            "mt_seconds":  round(mt_time, 4),
            "llm_seconds": round(parsed.llm_time, 4),
        },
    }

    # If the action was a reprompt, add reprompt + same-question audio URLs
    if result["action"] == "reprompt":
        response["reprompt_audio_url"] = _audio_url(question.reprompt_audio)
        response["question_audio_url"] = _audio_url(question.audio_file)

    # If call is complete and Q8 was answered, kick off post-call LLM in background
    if result["action"] == "complete":
        q8_turn = session.get_answer("Q8")
        if q8_turn and q8_turn.english_translation:
            # Fire-and-forget — don't block the response
            import asyncio
            asyncio.create_task(_run_post_call_q8(session.session_id, q8_turn.english_translation))

    return JSONResponse(response)


@router.post("/post_call/{session_id}",
    summary="Run post-call LLM analysis on Q8 recall answer",
    description="Extracts themes from the farmer's field day recall. Safe to call multiple times.")
async def run_post_call(session_id: str):
    try:
        session = SessionStore.get(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    q8_turn = session.get_answer("Q8")
    if not q8_turn:
        raise HTTPException(status_code=400, detail="Q8 was not answered in this session")

    analysis = await code_q8_themes(q8_turn.english_translation)
    session.post_call_analysis = analysis
    session._save()
    return JSONResponse({"session_id": session_id, "q8_analysis": analysis})


async def _run_post_call_q8(session_id: str, translation: str) -> None:
    """Background task — called automatically when call completes."""
    try:
        session = SessionStore.get(session_id)
        analysis = await code_q8_themes(translation)
        session.post_call_analysis = analysis
        session._save()
    except Exception:
        pass


@router.get("/session/{session_id}",
    summary="Get full session record")
async def get_session(session_id: str):
    try:
        session = SessionStore.get(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse(session.to_dict())


@router.get("/sessions",
    summary="List all sessions (summary)")
async def list_sessions():
    return JSONResponse(SessionStore.list_all())


@router.get("/questions",
    summary="List all questions and routing table (for inspection)")
async def list_questions():
    return JSONResponse({
        q_id: {
            "text":        q.text,
            "audio_file":  q.audio_file,
            "parser_type": q.parser_type,
            "options":     [{"key": o.key, "text": o.text} for o in q.options],
        }
        for q_id, q in QUESTIONS.items()
    })


# ---------------------------------------------------------------------------
# Audio file serving
# ---------------------------------------------------------------------------

@router.get("/audio/{filename}",
    summary="Serve a pre-recorded audio file",
    response_class=FileResponse)
async def serve_audio(filename: str):
    # Sanitise — no path traversal
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = AUDIOS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Audio file not found: {filename}")
    media_type = "audio/wav" if filename.endswith(".wav") else "audio/mpeg"
    return FileResponse(path=str(path), media_type=media_type)
