# Voice AI Survey Platform

A phone-call-style IVR (Interactive Voice Response) survey system. It calls back a farmer, asks a scripted set of questions in natural spoken language, understands their free-form spoken answers, and dynamically branches to the next question — all without any human operator on the line.

Built as two interchangeable front ends over one shared survey engine:

- **REST API** (FastAPI) — drive the survey by uploading audio files or plain text, useful for testing and for the browser demo.
- **Real-time voice agent** (LiveKit) — a live, phone/browser voice call with real-time speech segmentation, so a caller can actually talk to it.

## How it works

```
caller's voice
     │
     ▼
  ASR + MT            speech → local-language transcript → English translation
     │
     ▼
  Answer Parser        classify the answer against the current question's routing options
     │
     ▼
  State Machine         look up the next question in the routing table (branches on
     │                  the answer, skips sections, times out long calls)
     ▼
  TTS / pre-recorded audio     play the next question back to the caller
```

Each turn is logged with full transcripts, inference timings, and the parser's confidence score, so a call can be replayed and audited end to end.

### Multi-strategy answer parsing

Different question types need different parsing strategies — the engine picks the right one per question:

| Parser | Used for | Approach |
|---|---|---|
| `keyword` | Yes/No questions | Keyword + negation-aware matching (`"not planted"` ≠ `"planted"`) |
| `fuzzy` | Multi-option questions | Best-match scoring across keyword hits and token overlap |
| `regex` | Numeric answers | Extracts quantities from natural speech ("two bags", "15 acres") |
| `llm` | Open-ended short answers | Claude (via AWS Bedrock) extracts a structured entity (e.g. a product name) in real time |
| `raw` | Long free-form answers | Stored verbatim; themes are coded by an LLM *after* the call so it never adds latency to a live conversation |

Unclear answers get a single re-prompt before falling back to a default route, so the call never gets stuck.

### Routing & call-length control

The question graph is a simple declarative table (`survey/questions.py`) mapping `(question, answer_key) → next_question`, including conditional branches (e.g. skip the follow-up question if the farmer hasn't tried the product), a wildcard for open-ended questions, and a time-based rule that shortens the call if it's already run long — all validated against a corresponding [routing diagram](https://mermaid.js.org/) kept alongside the code.

### Session persistence

Sessions are stored as JSON on disk rather than in memory, so the state machine works correctly behind multiple Gunicorn workers — any worker can pick up any in-progress call.

## Stack

- **FastAPI** — REST API, audio serving, session inspection endpoints
- **LiveKit Agents** — real-time voice pipeline (custom STT/LLM/TTS pipeline nodes, energy-based voice activity segmentation)
- **Claude (Haiku 4.5, via AWS Bedrock)** — entity extraction and post-call theme coding
- **Custom ASR + MT service** — speech recognition and machine translation for a local language
- **Gunicorn + nginx** — production deployment with HTTPS

## Project layout

```
main.py                 FastAPI app entrypoint
routers/ivr_survey.py   REST endpoints (start/respond/inspect a survey call)
survey/questions.py     Question graph + routing table
survey/answer_parser.py Multi-strategy answer parsing (keyword/fuzzy/regex/LLM)
survey/session.py       Session state machine + disk-backed persistence
agent/agent.py          LiveKit real-time voice agent
static/index.html       Browser demo — visualises the pipeline live as a call runs
scripts/                Deployment & ops scripts (gunicorn/nginx, agent process manager, audio generation)
```

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # set ASR_MT_API_URL, AWS_BEARER_TOKEN_BEDROCK, etc.
./scripts/server.sh dev
```

Open `http://localhost:8000` for the browser demo, which plays out a full simulated call and shows every pipeline stage (ASR → MT → parser → state machine → TTS) live with latencies.

For the real-time voice agent:

```bash
cd agent && uv pip install -e .
../scripts/agent.sh dev
```

---

*Company and product names throughout this repo are placeholders — this was built as a client engagement and identifying details have been removed. The architecture, code, and demo are otherwise unmodified.*
