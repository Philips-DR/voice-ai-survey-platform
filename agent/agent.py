"""
AgriCo SeedX Survey Agent

Architecture:
  AgentSession (handles Playground protocol + VAD audio segmentation)
    └── stt_node   → Triton ASR+MT → English transcript
    └── llm_node   → survey state machine → next audio filename
    └── tts_node   → plays pre-recorded WAV file

The existing survey logic (questions.py, session.py, answer_parser.py) is
imported directly — the agent is just the phone/voice interface on top of it.
"""

import asyncio
import io
import logging
import os
import wave
from pathlib import Path
from typing import AsyncIterable

import httpx
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext, ModelSettings, WorkerOptions, cli, llm, stt, tts
from livekit.agents.voice.room_io import RoomOptions, TextInputOptions, TextInputEvent
from livekit.plugins import silero

logger = logging.getLogger("ivr_survey.agent")
from livekit.agents.llm.llm import LLMStream, DEFAULT_API_CONNECT_OPTIONS
from livekit.agents.stt import SpeechData, SpeechEvent, SpeechEventType, STTCapabilities
from livekit.agents.tts import TTSCapabilities

load_dotenv(Path(__file__).parent.parent / ".env")

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from survey.answer_parser import parse_answer
from survey.questions import QUESTIONS, SYS_AUDIO
from survey.session import SessionStore

AUDIOS_DIR   = Path(__file__).parent.parent / "audios"
ASR_MT_URL   = os.getenv("ASR_MT_API_URL", "http://localhost:8080").rstrip("/") + "/transcribe_and_translate/"
SAMPLE_RATE  = 16_000
NUM_CHANNELS = 1


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def frames_to_wav(frames: list[rtc.AudioFrame]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(NUM_CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        for f in frames:
            wf.writeframes(bytes(f.data))
    return buf.getvalue()


async def _wav_file_to_frames(path: Path) -> AsyncIterable[rtc.AudioFrame]:
    with wave.open(str(path), "rb") as wf:
        sample_rate  = wf.getframerate()
        num_channels = wf.getnchannels()
        raw          = wf.readframes(wf.getnframes())

    chunk_bytes = sample_rate * num_channels * 2 // 10  # 100 ms
    for offset in range(0, len(raw), chunk_bytes):
        chunk = raw[offset : offset + chunk_bytes]
        if not chunk:
            break
        yield rtc.AudioFrame(
            data=chunk,
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=len(chunk) // (num_channels * 2),
        )
        await asyncio.sleep(0.1)


async def transcribe_and_translate(wav_bytes: bytes) -> str:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                ASR_MT_URL,
                files={"audio_file": ("response.wav", wav_bytes, "audio/wav")},
            )
            resp.raise_for_status()
            data = resp.json()
            twi = data.get("transcription", "")
            eng = data.get("translation", "")
            logger.info("[asr] twi: %r", twi)
            logger.info("[mt]  eng: %r", eng)
            return eng
    except Exception as e:
        logger.error("[asr] ERROR: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Stubs — register plugins so AgentSession activates all pipeline nodes.
# The actual work is done in stt_node / llm_node / tts_node overrides.
# ---------------------------------------------------------------------------

class _StubSTT(stt.STT):
    """Registered so AgentSession sets self.stt and calls stt_node."""
    def __init__(self) -> None:
        super().__init__(capabilities=STTCapabilities(streaming=False, interim_results=False))

    async def _recognize_impl(self, buffer, **kwargs) -> SpeechEvent:  # type: ignore
        # Never called — stt_node override handles everything
        raise NotImplementedError


class _StubLLMStream(LLMStream):
    """Never runs — llm_node override handles everything."""
    async def _run(self) -> None:
        pass


class _StubLLM(llm.LLM):
    """Registered so AgentSession activates llm_node."""
    def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kw) -> LLMStream:
        return _StubLLMStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class _StubTTS(tts.TTS):
    """Registered so AgentSession sets self.tts and calls tts_node."""
    def __init__(self) -> None:
        super().__init__(
            capabilities=TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )

    async def synthesize(self, text: str, **kwargs):
        # Never called — tts_node override handles everything
        return  # type: ignore


# ---------------------------------------------------------------------------
# Survey Agent — overrides all three pipeline nodes
# ---------------------------------------------------------------------------

class SurveyAgent(Agent):

    def __init__(self, survey_session) -> None:
        super().__init__(instructions="")
        self.survey_session = survey_session

    # ── STT node: Triton ASR + MT ────────────────────────────────────────────
    # The framework passes a continuous audio channel that never closes.
    # We must detect end-of-utterance ourselves via energy-based silence detection,
    # then batch the speech frames and send to Triton.

    async def stt_node(
        self,
        audio: AsyncIterable[rtc.AudioFrame],
        model_settings: ModelSettings,
    ) -> AsyncIterable[SpeechEvent]:
        import struct

        ENERGY_THRESHOLD   = 400   # RMS — below this = silence
        END_SILENCE_S      = 1.2   # seconds of silence to end utterance
        MIN_SPEECH_S       = 0.3   # discard very short bursts (< 300ms)

        speech_frames: list[rtc.AudioFrame] = []
        silence_s     = 0.0
        in_speech     = False

        async for frame in audio:
            samples   = struct.unpack_from(f"{len(frame.data)//2}h", bytes(frame.data))
            rms       = (sum(s * s for s in samples) / max(len(samples), 1)) ** 0.5
            is_speech = rms > ENERGY_THRESHOLD

            if is_speech:
                in_speech = True
                silence_s = 0.0
                speech_frames.append(frame)
            elif in_speech:
                speech_frames.append(frame)
                silence_s += frame.duration
                if silence_s >= END_SILENCE_S:
                    # Enough silence — treat as end of utterance
                    total_s = sum(f.duration for f in speech_frames)
                    if total_s >= MIN_SPEECH_S:
                        wav = frames_to_wav(speech_frames)
                        eng = await transcribe_and_translate(wav)
                        if eng:
                            yield SpeechEvent(
                                type=SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[SpeechData(language="en", text=eng, confidence=0.9)],
                            )
                    speech_frames = []
                    silence_s     = 0.0
                    in_speech     = False

    # ── LLM node: survey state machine ──────────────────────────────────────

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[str]:
        if self.survey_session.is_complete:
            return

        # Get transcript from the last user message
        transcript = ""
        for msg in reversed(chat_ctx.messages()):
            if msg.role == "user":
                transcript = msg.text_content or ""
                break

        if not transcript:
            return

        question = self.survey_session.current_question()
        logger.info("[llm_node] transcript=%r  q=%s", transcript, question.id)
        parsed   = await parse_answer(question, transcript)
        logger.info("[parser] q=%s  key=%s  conf=%.2f", question.id, parsed.answer_key, parsed.confidence)

        result = self.survey_session.advance(parsed)
        action = result["action"]
        logger.info("[sm] action=%s  next=%s", action, result["next_question_id"])

        # Yield pipe-separated filenames so tts_node can play them in sequence
        if action == "reprompt":
            yield f"{question.reprompt_audio}|{question.audio_file}"
        else:
            audio_file = result.get("next_audio") or SYS_AUDIO["CLOSE"]
            yield audio_file

    # ── TTS node: play pre-recorded WAV files ────────────────────────────────

    async def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ) -> AsyncIterable[rtc.AudioFrame]:
        full_text = "".join([chunk async for chunk in text]).strip()
        if not full_text:
            return

        for filename in full_text.split("|"):
            path = AUDIOS_DIR / filename.strip()
            if not path.exists():
                logger.warning("[tts] WARNING: %s not found", path)
                continue
            logger.info("[tts] playing %s", filename.strip())
            async for frame in _wav_file_to_frames(path):
                yield frame


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    logger.info("[agent] Connected to room: %s", ctx.room.name)

    ctx.room.on("participant_connected",
                lambda p: logger.info("[room] participant_connected identity=%r", p.identity))

    survey_session = SessionStore.create()
    logger.info("[agent] Session %s", survey_session.session_id)

    # Custom text input callback — bypasses VAD silence gate for typed input
    async def _text_input_cb(sess: AgentSession, ev: TextInputEvent) -> None:
        logger.info("[input] text=%r from=%r", ev.text,
                    ev.participant.identity if ev.participant else "?")
        async with sess._claim_user_turn():
            await sess.interrupt(force=True)   # force=True overrides allow_interruptions=False
            sess.generate_reply(user_input=ev.text, allow_interruptions=False)

    session = AgentSession(
        stt=_StubSTT(),
        llm=_StubLLM(),
        vad=silero.VAD.load(),      # VAD needed to segment voice input for stt_node
        tts=_StubTTS(),
        tts_text_transforms=None,   # don't mangle audio filenames
    )
    await session.start(
        room=ctx.room,
        agent=SurveyAgent(survey_session),
        room_options=RoomOptions(
            text_input=TextInputOptions(text_input_cb=_text_input_cb),
        ),
    )

    # Play the opening question — session.say() routes through our tts_node
    first_audio = QUESTIONS[survey_session.current_question_id].audio_file
    logger.info("[agent] Playing opening: %s", first_audio)
    await session.say(first_audio)
    logger.info("[agent] Opening audio complete, waiting for input")

    # Keep alive until survey ends
    while not survey_session.is_complete:
        await asyncio.sleep(0.5)

    logger.info("[agent] Survey complete — %s", survey_session.completion_reason)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
