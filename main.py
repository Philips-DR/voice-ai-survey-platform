from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from routers.ivr_survey import router

app = FastAPI(
    title="Demeter BM270 IVR Survey",
    description=(
        "Voice IVR survey pipeline for Demeter Ghana Limited. "
        "Runs ASR → MT → answer parser → routing logic for the BM270 field day follow-up survey."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/survey", tags=["IVR Survey"])
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}
