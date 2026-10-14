from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

ROOT = Path(os.getenv("DATA_DIR", "/data"))
LIBRARY_DIR = ROOT / "library"
JOBS_DIR = ROOT / "jobs"
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
TTL_SECONDS = int(os.getenv("TEMP_TTL_SECONDS", "3600"))
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BIN", "ffprobe")
ALLOWED_ORIGINS = [x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()]
JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

# --- yt-dlp constants ---
YTDLP_SOCKET_TIMEOUT = int(os.getenv("YTDLP_SOCKET_TIMEOUT", "30"))
YTDLP_MAX_FILESIZE = os.getenv("YTDLP_MAX_FILESIZE", "2G")

# Regex to loosely validate a YouTube URL (public videos only)
_YT_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/(watch\?.*v=|shorts/|embed/)|youtu\.be/)[\w\-]{11}",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


async def read_upload(upload: UploadFile, dest: Path, limit: int = MAX_UPLOAD) -> int:
    total = 0
    with dest.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "Fichier trop volumineux")
            out.write(chunk)
    if total == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "Fichier vide")
    return total


def run_command(args: list[str], timeout: int = 7200) -> None:
    import subprocess

    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        detail = (result.stderr or "Echec du traitement media")[-3000:]
        raise RuntimeError(detail)


def probe_duration(path: Path) -> float:
    import subprocess

    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise RuntimeError("Format media non reconnu")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Duree media indeterminee") from exc
    if not (0 < duration < 24 * 3600):
        raise RuntimeError("Duree media invalide")
    return duration


def safe_title(name: str) -> str:
    return Path(name or "audio").stem[:120] or "audio"


def library_metadata_path() -> Path:
    return LIBRARY_DIR / "index.json"


def read_library() -> list[dict]:
    path = library_metadata_path()
    try:
        records = json.loads(path.read_text("utf-8")) if path.exists() else []
        return [
            r for r in records
            if isinstance(r, dict)
            and isinstance(r.get("id"), str)
            and re.fullmatch(r"[a-f0-9]{32}", r["id"])
            and (LIBRARY_DIR / f"{r['id']}.m4a").is_file()
        ]
    except (json.JSONDecodeError, OSError):
        return []


def write_library(records: list[dict]) -> None:
    target = library_metadata_path()
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), "utf-8")
    temp.replace(target)


LIBRARY_LOCK = asyncio.Lock()


async def cleanup_loop() -> None:
    while True:
        try:
            cutoff = datetime.now().timestamp() - TTL_SECONDS
            for item in JOBS_DIR.iterdir():
                with suppress(OSError):
                    if item.stat().st_mtime < cutoff:
                        shutil.rmtree(item) if item.is_dir() else item.unlink()
            for item in LIBRARY_DIR.glob(".*.upload"):
                with suppress(OSError):
                    if item.stat().st_mtime < cutoff:
                        item.unlink()
        except OSError:
            pass
        await asyncio.sleep(min(max(TTL_SECONDS // 4, 60), 900))


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_dirs()
    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="YouTube Mix -- traitement de medias autorises", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Range"],
    expose_headers=["Accept-Ranges", "Content-Range", "Content-Length"],
)


# --- Pydantic models ---

class ProcessResult(BaseModel):
    video_id: str
    stream_url: str
    duration_seconds: float
    expires_in_seconds: int


class YoutubeImportResult(BaseModel):
    id: str
    title: str
    duration_seconds: float
    created_at: str


class YoutubeProcessRequest(BaseModel):
    video_url: str
    audio_url: str = ""
    volume: float = 1.0
    audio_id: str | None = None  # optional: reuse a library track for audio


class YoutubeProcessResult(BaseModel):
    video_id: str
    stream_url: str
    duration_seconds: float
    expires_in_seconds: int
    video_title: str


# --- yt-dlp helpers ---

def _validate_yt_url(url: str, field: str = "url") -> str:
    """Raise HTTPException 422 if the URL does not look like a public YouTube URL."""
    if not _YT_URL_RE.match(url.strip()):
        raise HTTPException(422, f"URL YouTube invalide pour le champ '{field}'.")
    return url.strip()


def init_cookies() -> Path | None:
    """Load or initialize cookies from environment or file."""
    raw_cookies = os.getenv("YOUTUBE_COOKIES")
    if raw_cookies and raw_cookies.strip():
        target = Path(os.getenv("DATA_DIR", "/data")) / "cookies.txt"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(raw_cookies.strip(), encoding="utf-8")
            return target
        except OSError:
            pass
    custom_path = Path(os.getenv("COOKIES_FILE", "/data/cookies.txt"))
    if custom_path.is_file() and custom_path.stat().st_size > 0:
        return custom_path
    local_path = Path(__file__).parent / "cookies.txt"
    if local_path.is_file() and local_path.stat().st_size > 0:
        return local_path
    return None


def _ytdlp_base_opts(quiet: bool = False) -> dict:
    """Common yt-dlp options shared by all extraction calls."""
    opts = {
        "noplaylist": True,
        "quiet": quiet,
        "no_warnings": quiet,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "mweb", "android", "web"]
            }
        },
    }
    cookie_path = init_cookies()
    if cookie_path and cookie_path.is_file():
        opts["cookiefile"] = str(cookie_path)
    return opts


def extract_video_stream(url: str) -> dict:
    """
    Extract the best H.264/MP4 video stream URL from a YouTube URL.

    Returns a dict with keys: url, title, duration.
    Raises RuntimeError on any yt-dlp error.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    opts = {
        **_ytdlp_base_opts(quiet=True),
        # Best H.264 MP4 video -- fallback to format 18 or any video stream
        "format": (
            "bestvideo[vcodec^=avc][ext=mp4]"
            "/bestvideo[ext=mp4]"
            "/bestvideo[vcodec^=avc]"
            "/bestvideo"
            "/best[height<=720]"
            "/18"
            "/best"
            "/bv*"
            "/b"
        ),
        "skip_download": True,
    }

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        msg = str(exc)
        if "Sign in to confirm you" in msg or "bot" in msg.lower():
            raise RuntimeError(
                "YouTube bloque les serveurs cloud distants (anti-robot). "
                "Solutions : 1) Renseignez vos cookies YouTube dans Render (variable YOUTUBE_COOKIES), "
                "ou 2) Lancez le backend en local sur votre Mac sans aucune restriction."
            ) from exc
        if "Private video" in msg or "This video is private" in msg:
            raise RuntimeError("Video privee ou inaccessible.") from exc
        if "Video unavailable" in msg or "has been removed" in msg or "This video is unavailable" in msg:
            raise RuntimeError("Video supprimee ou indisponible. Verifiez l'identifiant de la video.") from exc
        if "Failed to extract any player response" in msg:
            raise RuntimeError("Impossible d'extraire la video YouTube. Verifiez que l'URL est correcte (attention a la confusion entre le chiffre 0 et la lettre O).") from exc
        raise RuntimeError(f"Impossible d'extraire le flux video : {msg[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"Erreur inattendue yt-dlp (video) : {str(exc)[:500]}") from exc

    if info is None:
        raise RuntimeError("yt-dlp n'a retourne aucune information pour cette video.")

    stream_url = info.get("url") or (info.get("formats") or [{}])[-1].get("url", "")
    if not stream_url:
        raise RuntimeError("Aucun flux video trouve pour cette URL.")

    return {
        "url": stream_url,
        "title": info.get("title", "Video sans titre"),
        "duration": float(info.get("duration") or 0),
    }


def download_audio_to_library(url: str, audio_id: str) -> dict:
    """
    Download the best audio stream from url and save it as
    LIBRARY_DIR/<audio_id>.m4a (AAC/M4A, max 20 min).

    Returns a dict with keys: title, duration_seconds.
    Raises RuntimeError on any yt-dlp / ffmpeg error.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    raw_output = LIBRARY_DIR / f".{audio_id}.ytdl"
    raw_stem = str(raw_output.with_suffix(""))

    opts = {
        **_ytdlp_base_opts(quiet=False),
        # Best audio stream, fallback to best overall stream (e.g. format 18)
        "format": "bestaudio/best",
        "outtmpl": raw_stem + ".%(ext)s",
        "max_filesize": YTDLP_MAX_FILESIZE,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
        }],
    }

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        msg = str(exc)
        if "Sign in to confirm you" in msg or "bot" in msg.lower():
            raise RuntimeError(
                "YouTube bloque les serveurs cloud distants (anti-robot). "
                "Solutions : 1) Renseignez vos cookies YouTube dans Render (variable YOUTUBE_COOKIES), "
                "ou 2) Lancez le backend en local sur votre Mac sans aucune restriction."
            ) from exc
        if "Private video" in msg or "This video is private" in msg:
            raise RuntimeError("Video privee ou inaccessible.") from exc
        if "Video unavailable" in msg or "has been removed" in msg or "This video is unavailable" in msg:
            raise RuntimeError("Video supprimee ou indisponible. Verifiez l'identifiant de la video.") from exc
        if "Failed to extract any player response" in msg:
            raise RuntimeError("Impossible de lire les flux YouTube. Verifiez que l'URL est correcte (attention a la confusion entre le chiffre 0 et la lettre O).") from exc
        raise RuntimeError(f"Impossible de telecharger l'audio : {msg[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"Erreur inattendue yt-dlp (audio) : {str(exc)[:500]}") from exc

    if info is None:
        raise RuntimeError("yt-dlp n'a retourne aucune information pour cet audio.")

    title = info.get("title", "Audio sans titre")
    yt_duration = float(info.get("duration") or 0)

    # Find the downloaded file (yt-dlp chose the extension)
    downloaded: Path | None = None
    for ext in ("m4a", "webm", "opus", "mp3", "aac", "ogg", "wav", "mp4", "mkv"):
        candidate = raw_output.with_suffix(f".{ext}")
        if candidate.is_file():
            downloaded = candidate
            break
    # Fallback: glob for any file matching the stem
    if downloaded is None:
        candidates = list(LIBRARY_DIR.glob(f".{audio_id}.ytdl.*"))
        downloaded = candidates[0] if candidates else None

    if downloaded is None or not downloaded.is_file():
        raise RuntimeError("Le fichier audio telecharge est introuvable apres le telechargement.")

    # Normalise to M4A/AAC with a hard 20-minute cap via FFmpeg
    normalized = LIBRARY_DIR / f"{audio_id}.m4a"
    try:
        run_command([
            FFMPEG, "-y", "-v", "error",
            "-i", str(downloaded),
            "-t", "1200",
            "-vn", "-c:a", "aac", "-b:a", "192k", "-f", "ipod",
            str(normalized),
        ])
    finally:
        downloaded.unlink(missing_ok=True)

    actual_duration = probe_duration(normalized)
    return {
        "title": title[:120],
        "duration_seconds": round(min(actual_duration, 1200.0), 2),
        "yt_duration": yt_duration,
    }


# --- Existing endpoints (unchanged) ---

@app.get("/health")
def health():
    try:
        from yt_dlp import version as ytdlp_version
        yt_version = ytdlp_version.__version__
        yt_status = "ok"
    except Exception:
        yt_version = "unknown"
        yt_status = "not_installed"
    cookie_path = init_cookies()
    has_cookies = cookie_path is not None and cookie_path.is_file() and cookie_path.stat().st_size > 0
    return {
        "status": "ok",
        "youtube_import": yt_status,
        "ytdlp_version": yt_version,
        "cookies_loaded": has_cookies,
    }


@app.get("/debug/formats")
def debug_formats(video_id: str = "WE8TinxNPb0"):
    from yt_dlp import YoutubeDL
    opts = _ytdlp_base_opts(quiet=True)
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return {
                "title": info.get("title"),
                "cookies_found": bool(opts.get("cookiefile")),
                "formats": [
                    {
                        "id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                        "protocol": f.get("protocol"),
                        "has_url": bool(f.get("url")),
                    }
                    for f in info.get("formats", [])
                ],
            }
        except Exception as e:
            return {"error": str(e)}


@app.get("/library")
def list_library():
    ensure_dirs()
    return read_library()


@app.get("/library/{audio_id}/audio")
def get_library_audio(audio_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", audio_id):
        raise HTTPException(404, "Piste audio introuvable")
    path = LIBRARY_DIR / f"{audio_id}.m4a"
    if not path.is_file():
        raise HTTPException(404, "Piste audio introuvable")
    return FileResponse(path, media_type="audio/mp4", filename="audio.m4a")


@app.post("/library")
async def add_library_audio(file: Annotated[UploadFile, File(...)], title: Annotated[str | None, Form()] = None):
    ensure_dirs()
    audio_id = uuid.uuid4().hex
    raw = LIBRARY_DIR / f".{audio_id}.upload"
    normalized = LIBRARY_DIR / f"{audio_id}.m4a"
    try:
        await read_upload(file, raw)
        duration = await asyncio.to_thread(probe_duration, raw)
        if duration <= 0:
            raise RuntimeError("Duree audio invalide")
        await asyncio.to_thread(run_command, [FFMPEG, "-y", "-v", "error", "-i", str(raw), "-t", "1200", "-vn", "-c:a", "aac", "-b:a", "192k", "-f", "ipod", str(normalized)])
        duration = min(await asyncio.to_thread(probe_duration, normalized), 1200.0)
        record = {"id": audio_id, "title": (title or safe_title(file.filename or "audio"))[:120], "duration_seconds": round(duration, 2), "created_at": utc_now()}
        async with LIBRARY_LOCK:
            write_library([*read_library(), record])
        return record
    except HTTPException:
        normalized.unlink(missing_ok=True)
        raise
    except Exception as exc:
        normalized.unlink(missing_ok=True)
        raise HTTPException(422, "Audio invalide ou traitement impossible") from exc
    finally:
        raw.unlink(missing_ok=True)
        await file.close()


@app.post("/process", response_model=ProcessResult)
async def process_mix(
    video: Annotated[UploadFile, File(...)],
    audio_id: Annotated[str | None, Form()] = None,
    audio: Annotated[UploadFile | None, File()] = None,
    volume: Annotated[float, Form(ge=0, le=2)] = 1.0,
):
    ensure_dirs()
    if bool(audio_id) == bool(audio):
        raise HTTPException(400, "Choisir une piste de bibliotheque ou importer un audio")
    job_id = uuid.uuid4().hex
    work = JOBS_DIR / job_id
    work.mkdir()
    video_path = work / "source-video"
    audio_path = work / "source-audio.m4a"
    output_path = work / "mix.mp4"
    try:
        await read_upload(video, video_path)
        if audio_id:
            if not re.fullmatch(r"[a-f0-9]{32}", audio_id):
                raise HTTPException(400, "Identifiant audio invalide")
            candidate = LIBRARY_DIR / f"{audio_id}.m4a"
            if not candidate.is_file():
                raise HTTPException(404, "Piste audio introuvable")
            shutil.copyfile(candidate, audio_path)
        else:
            assert audio is not None
            raw_audio = work / "uploaded-audio"
            await read_upload(audio, raw_audio)
            try:
                source_duration = await asyncio.to_thread(probe_duration, raw_audio)
                if source_duration <= 0:
                    raise RuntimeError("Duree audio invalide")
                await asyncio.to_thread(run_command, [FFMPEG, "-y", "-v", "error", "-i", str(raw_audio), "-t", "1200", "-vn", "-c:a", "aac", "-b:a", "192k", "-f", "ipod", str(audio_path)])
            finally:
                raw_audio.unlink(missing_ok=True)
        async with JOB_SEMAPHORE:
            video_duration, audio_duration = await asyncio.gather(
                asyncio.to_thread(probe_duration, video_path),
                asyncio.to_thread(probe_duration, audio_path),
            )
            if audio_duration > 1200.5:
                raise RuntimeError("La piste audio depasse la limite de 20 minutes")
            await asyncio.to_thread(run_command, [
                FFMPEG, "-y", "-v", "error", "-i", str(video_path), "-stream_loop", "-1", "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0", "-t", f"{video_duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-vf", "scale=w='min(1920,iw)':h=-2", "-c:a", "aac", "-b:a", "192k",
                "-af", f"volume={volume:.3f}", "-movflags", "+faststart", "-f", "mp4", str(output_path),
            ])
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
        return ProcessResult(video_id=job_id, stream_url=f"/stream/{job_id}", duration_seconds=video_duration, expires_in_seconds=TTL_SECONDS)
    except HTTPException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(422, "Echec du mixage. Verifiez les formats et codecs des medias.") from exc
    finally:
        await video.close()
        if audio is not None:
            await audio.close()


@app.get("/stream/{video_id}")
def stream_mix(video_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", video_id):
        raise HTTPException(404, "Video introuvable")
    path = JOBS_DIR / video_id / "mix.mp4"
    if not path.is_file():
        raise HTTPException(404, "Video introuvable ou expiree")
    return FileResponse(path, media_type="video/mp4", filename="mix.mp4", content_disposition_type="inline")


# --- YouTube endpoints (new) ---

@app.post("/youtube/import-audio", response_model=YoutubeImportResult)
async def youtube_import_audio(
    audio_url: Annotated[str, Form()],
    title: Annotated[str | None, Form()] = None,
):
    """
    Download the best audio stream from audio_url (YouTube), normalise it to
    AAC/M4A (max 20 min) and persist it in the library.

    The track can then be reused by its id in /youtube/process or the
    standard /process endpoint.
    """
    ensure_dirs()
    _validate_yt_url(audio_url, "audio_url")

    audio_id = uuid.uuid4().hex
    normalized = LIBRARY_DIR / f"{audio_id}.m4a"
    try:
        result = await asyncio.to_thread(download_audio_to_library, audio_url, audio_id)
        display_title = (title.strip()[:120] if title and title.strip() else result["title"])
        record = {
            "id": audio_id,
            "title": display_title,
            "duration_seconds": result["duration_seconds"],
            "created_at": utc_now(),
            "source": "youtube",
        }
        async with LIBRARY_LOCK:
            write_library([*read_library(), record])
        return YoutubeImportResult(**record)
    except HTTPException:
        normalized.unlink(missing_ok=True)
        raise
    except RuntimeError as exc:
        normalized.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        normalized.unlink(missing_ok=True)
        raise HTTPException(500, f"Erreur lors de l'import audio YouTube : {str(exc)[:300]}") from exc


@app.post("/youtube/process", response_model=YoutubeProcessResult)
async def youtube_process(body: YoutubeProcessRequest):
    """
    Full pipeline from two YouTube URLs:

    1. Extract the best H.264/MP4 video stream from video_url (no download,
       FFmpeg reads the stream URL directly).
    2. Either use audio_id (library track) or extract + download the best
       audio from audio_url, normalise it (AAC, <=20 min).
    3. Mix with FFmpeg:
       - Re-encode video (H.264, <=1920 px wide, yuv420p).
       - Loop audio if shorter than video, truncate at 20 min.
       - Apply volume adjustment.
       - Output iOS-friendly MP4 with faststart.
    """
    ensure_dirs()

    video_url = _validate_yt_url(body.video_url, "video_url")
    volume = max(0.0, min(2.0, float(body.volume)))

    # Validate audio source
    audio_id = body.audio_id
    if audio_id:
        if not re.fullmatch(r"[a-f0-9]{32}", audio_id):
            raise HTTPException(400, "Identifiant audio invalide")
        if not (LIBRARY_DIR / f"{audio_id}.m4a").is_file():
            raise HTTPException(404, "Piste audio introuvable dans la bibliotheque")
    else:
        if not body.audio_url:
            raise HTTPException(400, "Fournissez une URL audio YouTube ou un audio_id de bibliotheque.")
        _validate_yt_url(body.audio_url, "audio_url")

    job_id = uuid.uuid4().hex
    work = JOBS_DIR / job_id
    work.mkdir()
    audio_path = work / "source-audio.m4a"
    output_path = work / "mix.mp4"

    # Temporary audio_id for a one-off download (not persisted to library)
    tmp_audio_id: str | None = None

    try:
        # Step 1: extract video stream URL
        try:
            video_info = await asyncio.to_thread(extract_video_stream, video_url)
        except RuntimeError as exc:
            raise HTTPException(422, f"Video YouTube : {exc}") from exc

        video_stream_url = video_info["url"]
        video_title = video_info["title"]

        # Step 2: prepare audio
        if audio_id:
            # Reuse existing library track
            shutil.copyfile(LIBRARY_DIR / f"{audio_id}.m4a", audio_path)
        else:
            # Download audio into a temporary slot (not saved to library index)
            tmp_audio_id = uuid.uuid4().hex
            try:
                await asyncio.to_thread(download_audio_to_library, body.audio_url, tmp_audio_id)
            except RuntimeError as exc:
                raise HTTPException(422, f"Audio YouTube : {exc}") from exc
            tmp_file = LIBRARY_DIR / f"{tmp_audio_id}.m4a"
            shutil.move(str(tmp_file), str(audio_path))

        # Step 3: FFmpeg mix
        async with JOB_SEMAPHORE:
            audio_duration = await asyncio.to_thread(probe_duration, audio_path)
            if audio_duration > 1200.5:
                raise HTTPException(422, "La piste audio depasse la limite de 20 minutes")

            # FFmpeg reads video directly from the yt-dlp stream URL
            await asyncio.to_thread(run_command, [
                FFMPEG, "-y", "-v", "error",
                # Video: streamed directly from YouTube CDN
                "-i", video_stream_url,
                # Audio: local normalised file, looped if needed
                "-stream_loop", "-1", "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0",
                # Truncate output at 20 min
                "-t", "1200",
                # Video codec: H.264 for maximum iOS compatibility
                "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-vf", "scale=w='min(1920,iw)':h=-2",
                # Audio codec: AAC with volume adjustment
                "-c:a", "aac", "-b:a", "192k",
                "-af", f"volume={volume:.3f}",
                # Streaming-optimised MP4
                "-movflags", "+faststart",
                "-f", "mp4", str(output_path),
            ])

        audio_path.unlink(missing_ok=True)
        output_duration = await asyncio.to_thread(probe_duration, output_path)

        return YoutubeProcessResult(
            video_id=job_id,
            stream_url=f"/stream/{job_id}",
            duration_seconds=round(output_duration, 2),
            expires_in_seconds=TTL_SECONDS,
            video_title=video_title,
        )

    except HTTPException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(422, f"Echec du mixage YouTube : {str(exc)[:400]}") from exc
    finally:
        # Clean up any orphan temp audio file that was not moved
        if tmp_audio_id:
            (LIBRARY_DIR / f"{tmp_audio_id}.m4a").unlink(missing_ok=True)


# --- Generic error handler ---

@app.exception_handler(Exception)
async def internal_error_handler(_, exc: Exception):
    # Keep internal details out of generic errors; endpoint validation includes bounded ffmpeg diagnostics.
    return JSONResponse(status_code=500, content={"detail": "Erreur interne du serveur"})
