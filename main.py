import asyncio
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import filetype
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# ── Config ────────────────────────────────────────────────────────────────────
UPLOAD_DIR = Path("storage/uploads")
CONVERTED_DIR = Path("storage/converted")
MAX_FILE_BYTES = 150 * 1024 * 1024   # 150 MB per file
MAX_BATCH_SIZE = 10                   # files per request
FILE_TTL_SECONDS = 300               # auto-delete after 5 min
CLEANUP_INTERVAL = 60                # scan every 60 s
MAX_CONCURRENT = 4                   # simultaneous ffmpeg processes
FFMPEG_TIMEOUT = 300                 # kill hung ffmpeg after 5 min

SUPPORTED_OUTPUT_FORMATS = frozenset({"flac", "wav", "mp3", "ogg", "aac"})
LOSSLESS_FORMATS = frozenset({"flac", "wav"})
FORMAT_MIME = {
    "flac": "audio/flac",
    "wav":  "audio/wav",
    "mp3":  "audio/mpeg",
    "ogg":  "audio/ogg",
    "aac":  "audio/aac",
}
VALID_SAMPLE_RATES = frozenset({8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 192000})
VALID_CHANNELS = frozenset({1, 2})
VALID_BIT_RATES = frozenset({"64k", "96k", "128k", "192k", "256k", "320k"})
# Containers that are commonly audio-only (filetype returns video/* for these)
ALLOWED_MIME_PREFIXES = ("audio/", "video/webm", "video/mp4", "video/x-ms-asf", "video/ogg")

# ── In-memory job registry ────────────────────────────────────────────────────
# job_id -> {"expiry": monotonic_float, "path": Path, "name": str, "mime": str}
_registry: dict[str, dict] = {}
_semaphore: asyncio.Semaphore  # set in lifespan
_start_time: float = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────
def _safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[^\w\- ]", "_", stem).strip(". ")
    return stem[:100] or "audio"


def _cleanup_job(job_id: str) -> None:
    entry = _registry.pop(job_id, None)
    if entry:
        entry["path"].unlink(missing_ok=True)


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.monotonic()
        for jid in [k for k, v in list(_registry.items()) if now > v["expiry"]]:
            _cleanup_job(jid)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    global _semaphore, _start_time
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    # Purge orphaned job files left by a previous crash (skip .gitkeep etc.)
    for orphan in (*UPLOAD_DIR.iterdir(), *CONVERTED_DIR.iterdir()):
        if not orphan.name.startswith("."):
            orphan.unlink(missing_ok=True)
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    _start_time = time.monotonic()
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Audio Encoder", lifespan=lifespan)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'"
        )
        return resp


app.add_middleware(SecurityHeadersMiddleware)


# ── Per-file conversion ───────────────────────────────────────────────────────
async def _convert_one(
    file: UploadFile,
    target_format: str,
    channels: Optional[int],
    sample_rate: Optional[int],
    bit_rate: Optional[str],
) -> dict:
    contents = await file.read()

    if len(contents) > MAX_FILE_BYTES:
        return {"original": file.filename, "error": "Exceeds 150 MB limit"}

    # Validate by magic bytes, not extension
    kind = filetype.guess(contents[:262])
    if kind is None or not any(kind.mime.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        detected = kind.mime if kind else "unknown"
        return {"original": file.filename, "error": f"Not a recognised audio file (detected: {detected})"}

    job_id = uuid.uuid4().hex
    in_suffix = Path(file.filename).suffix.lower() or f".{kind.extension}"
    # Save locally for now since it's for personal use, maybe move to s3 later
    in_path = UPLOAD_DIR / f"{job_id}{in_suffix}"
    out_path = CONVERTED_DIR / f"{job_id}.{target_format}"
    download_name = f"{_safe_stem(file.filename)}.{target_format}"

    in_path.write_bytes(contents)

    cmd = ["ffmpeg", "-y", "-i", str(in_path)]
    if channels is not None:
        cmd += ["-ac", str(channels)]
    if sample_rate is not None:
        cmd += ["-ar", str(sample_rate)]
    if bit_rate is not None and target_format not in LOSSLESS_FORMATS:
        cmd += ["-b:a", bit_rate]
    cmd.append(str(out_path))

    async with _semaphore:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            in_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
            return {"original": file.filename, "error": "Conversion timed out — file may be too large or malformed"}

    in_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        out_path.unlink(missing_ok=True)
        return {"original": file.filename, "error": "Conversion failed — file may be corrupted or unsupported"}

    _registry[job_id] = {
        "expiry": time.monotonic() + FILE_TTL_SECONDS,
        "path": out_path,
        "name": download_name,
        "mime": FORMAT_MIME[target_format],
    }

    return {
        "original": file.filename,
        "job_id": job_id,
        "stream_url": f"/stream/{job_id}",
        "download_url": f"/download/{job_id}",
        "download_name": download_name,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.post("/convert")
async def convert(
    files: List[UploadFile] = File(...),
    target_format: str = Form(...),
    channels: Optional[int] = Form(None),
    sample_rate: Optional[int] = Form(None),
    bit_rate: Optional[str] = Form(None),
):
    if target_format not in SUPPORTED_OUTPUT_FORMATS:
        raise HTTPException(400, f"Unsupported format. Choose from: {', '.join(sorted(SUPPORTED_OUTPUT_FORMATS))}")
    if not files:
        raise HTTPException(400, "No files provided")
    if len(files) > MAX_BATCH_SIZE:
        raise HTTPException(400, f"Max {MAX_BATCH_SIZE} files per request")
    if channels is not None and channels not in VALID_CHANNELS:
        raise HTTPException(400, "channels must be 1 (mono) or 2 (stereo)")
    if sample_rate is not None and sample_rate not in VALID_SAMPLE_RATES:
        raise HTTPException(400, f"sample_rate must be one of: {sorted(VALID_SAMPLE_RATES)}")
    if bit_rate is not None and bit_rate not in VALID_BIT_RATES:
        raise HTTPException(400, f"bit_rate must be one of: {sorted(VALID_BIT_RATES)}")

    raw = await asyncio.gather(
        *(_convert_one(f, target_format, channels, sample_rate, bit_rate) for f in files),
        return_exceptions=True,
    )
    results = [
        r if isinstance(r, dict) else {"original": files[i].filename, "error": "Internal error"}
        for i, r in enumerate(raw)
    ]
    return {"results": results}


@app.get("/stream/{job_id}")
def stream(job_id: str):
    """Serve audio for in-browser playback. Supports Range requests for seeking. Does not delete the file."""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "Not found")
    entry = _registry.get(job_id)
    if not entry or not entry["path"].exists():
        raise HTTPException(404, "Not found")
    return FileResponse(entry["path"], media_type=entry["mime"])


@app.get("/download/{job_id}")
def download(job_id: str, background_tasks: BackgroundTasks):
    # Strict job_id validation prevents any path traversal
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "Not found")

    entry = _registry.get(job_id)
    if not entry or not entry["path"].exists():
        _registry.pop(job_id, None)
        raise HTTPException(404, "File not found or already downloaded")

    # Delete after the response is fully sent
    background_tasks.add_task(_cleanup_job, job_id)

    return FileResponse(
        entry["path"],
        filename=entry["name"],
        media_type="application/octet-stream",
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "active_jobs": len(_registry),
        "uptime_seconds": int(time.monotonic() - _start_time),
    }


@app.get("/formats")
def formats():
    return {
        "supported_output_formats": sorted(SUPPORTED_OUTPUT_FORMATS),
        "valid_channels": sorted(VALID_CHANNELS),
        "valid_sample_rates": sorted(VALID_SAMPLE_RATES),
        "valid_bit_rates": sorted(VALID_BIT_RATES),
        "note": "bit_rate is ignored for lossless formats (flac, wav)",
    }


# Mounted last so all API routes above take precedence
app.mount("/", StaticFiles(directory="static", html=True), name="static")
