"""
Ref-Video-to-Video API

Workflow per request:
  1. Extract first frame from uploaded ref_video  (ffmpeg)
  2. Convert character_images + first_frame to base64 data-URLs
  3. Call Seedream to generate a pose-matched image
     ref order: [char_1, char_2, char_3, first_frame]
  4. Call Parrot /animate with (pose_image + ref_video)
  5. Return job_id immediately; poll GET /status/{job_id}
"""

import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
import uuid
from io import BytesIO
from typing import List

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image as PILImage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ─── Hardcoded credentials ────────────────────────────────────────────────────

PARROT_API_KEY      = "pika_dkc-LaUeI6lsL5MP1aBjgX4ph_7zCi8kKiGhKZ0MMcA"
PARROT_ANIMATE_URL  = "https://parrot-test.pika.art/api/v1/generate/v0/animate"
PARROT_POLL_BASE    = "https://parrot-test.pika.art/api/v1/generate/v0"

SEEDREAM_API_KEY = "72021f63-9cd0-427a-9072-af185df35e86"
SEEDREAM_URL     = "https://ark.ap-southeast.bytepluses.com/api/v3/images/generations"
SEEDREAM_MODEL   = "seedream-4-5-251128"

TIKHUB_API_KEY   = "i4o6Jco30Wb6oWmWMr7Iyw3fF2edBrg56C7GnkncDcXmiESzOnOPeDjDAQ=="
TIKHUB_BASE      = "https://api.tikhub.io"

# ─── In-memory job store (survives process lifetime only) ─────────────────────

_jobs: dict[str, dict] = {}

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Ref-Video-to-Video API",
    description="Upload a reference video + character images → get back an animated video.",
    version="1.0.0",
)

# ─── Utilities ────────────────────────────────────────────────────────────────

def _extract_first_frame(video_bytes: bytes, ext: str = ".mp4") -> bytes:
    """Run ffmpeg to pull first frame; return PNG bytes."""
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as fv:
        fv.write(video_bytes)
        vid_path = fv.name
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as ff:
        frame_path = ff.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", vid_path, "-vframes", "1", "-f", "image2", frame_path],
            check=True, capture_output=True,
        )
        with open(frame_path, "rb") as f:
            return f.read()
    finally:
        for p in (vid_path, frame_path):
            if os.path.exists(p):
                os.unlink(p)


def _get_duration(video_bytes: bytes, ext: str = ".mp4") -> float:
    """Return video duration in seconds via ffprobe."""
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as fv:
        fv.write(video_bytes)
        vid_path = fv.name
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", vid_path],
            capture_output=True, text=True,
        )
        return float(json.loads(out.stdout).get("format", {}).get("duration", 0))
    except Exception:
        return 0.0
    finally:
        if os.path.exists(vid_path):
            os.unlink(vid_path)


def _duration_suffix(prompt: str, duration: float) -> str:
    secs = 15 if duration > 10 else (10 if duration > 5 else 5)
    stripped = prompt.strip()
    return f"{stripped} --{secs}sec" if stripped else f"--{secs}sec"


def _to_jpeg_data_url(image_bytes: bytes) -> str:
    """Convert any image bytes → JPEG base64 data-URL (strips alpha)."""
    img = PILImage.open(BytesIO(image_bytes))
    if img.mode in ("RGBA", "LA", "P"):
        bg = PILImage.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


# ─── TikHub downloader ────────────────────────────────────────────────────────

def _tikhub_headers() -> dict:
    return {"Authorization": f"Bearer {TIKHUB_API_KEY}"}


def _instagram_shortcode(url: str) -> str:
    """Extract shortcode from Instagram URL."""
    import re
    m = re.search(r"/(?:p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot extract Instagram shortcode from: {url}")
    return m.group(1)


def _extract_item(data: dict) -> dict | None:
    """Extract first item from TikHub Instagram response data."""
    if not data:
        return None
    items = data.get("items") or []
    return items[0] if items else None


async def _download_instagram(url: str) -> tuple[bytes, bool]:
    """Download Instagram media. Returns (bytes, is_image).
    Tries v3 API first (best for reels), falls back to v1 for image posts.
    """
    shortcode = _instagram_shortcode(url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        # Step 1: shortcode → media_id
        r1 = await client.get(
            f"{TIKHUB_BASE}/api/v1/instagram/v1/shortcode_to_media_id",
            params={"shortcode": shortcode},
            headers=_tikhub_headers(),
        )
        r1.raise_for_status()
        media_id = r1.json()["data"]["media_id"]

        # Step 2a: try v3 (works well for reels/videos)
        r2 = await client.get(
            f"{TIKHUB_BASE}/api/v1/instagram/v3/get_post_info",
            params={"media_id": media_id, "url": url},
            headers=_tikhub_headers(),
        )
        r2.raise_for_status()
        item = _extract_item(r2.json().get("data"))

        # Step 2b: fallback to v1 for image posts (v3 returns null data)
        if not item:
            r3 = await client.get(
                f"{TIKHUB_BASE}/api/v1/instagram/v1/fetch_post_by_url",
                params={"post_url": url},
                headers=_tikhub_headers(),
            )
            r3.raise_for_status()
            item = _extract_item(r3.json().get("data"))

        if not item:
            raise ValueError("TikHub could not fetch Instagram post info")

        video_versions = item.get("video_versions") or []
        if video_versions:
            media_url = video_versions[0]["url"]
            is_image = False
        else:
            candidates = (item.get("image_versions2") or {}).get("candidates") or []
            if not candidates:
                raise ValueError("No media found in Instagram post")
            media_url = candidates[0]["url"]
            is_image = True

        dl = await client.get(media_url)
        dl.raise_for_status()
        return dl.content, is_image


async def _download_tiktok(url: str) -> bytes:
    """Download TikTok video bytes via TikHub API."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True) as client:
        r = await client.get(
            f"{TIKHUB_BASE}/api/v1/tiktok/app/v3/fetch_one_video_by_share_url",
            params={"share_url": url},
            headers=_tikhub_headers(),
        )
        r.raise_for_status()
        aweme = r.json()["data"]["aweme_detail"]
        video = aweme.get("video", {})

        # Prefer no-watermark, fallback to play_addr
        def _first_url(key: str) -> str | None:
            return (video.get(key) or {}).get("url_list", [None])[0]

        video_url = (
            _first_url("download_no_watermark_addr")
            or _first_url("play_addr")
            or _first_url("download_addr")
        )
        if not video_url:
            raise ValueError(f"TikHub returned no video URL for TikTok.")

        dl = await client.get(video_url)
        dl.raise_for_status()
        return dl.content


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}


def _detect_is_image(filename: str, content_type: str = "") -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in _IMAGE_EXTS or content_type.lower() in _IMAGE_CONTENT_TYPES


async def _download_from_social_url(url: str) -> tuple[bytes, str, bool]:
    """Auto-detect platform and return (bytes, ext, is_image)."""
    url_lower = url.lower()
    if "instagram.com" in url_lower:
        logger.info("Downloading Instagram media: %s", url)
        content, is_image = await _download_instagram(url)
        ext = ".jpg" if is_image else ".mp4"
        return content, ext, is_image
    elif "tiktok.com" in url_lower:
        logger.info("Downloading TikTok video: %s", url)
        return await _download_tiktok(url), ".mp4", False
    else:
        raise ValueError("Unsupported URL platform. Only Instagram and TikTok are supported.")


# ─── API callers ──────────────────────────────────────────────────────────────

async def _call_seedream(
    prompt: str,
    data_urls: list[str],
    width: int,
    height: int,
) -> bytes:
    """Generate pose-matched image with Seedream; return JPEG bytes."""
    # Seedream OpenAI-compat mode requires >= 3,686,400 px
    min_px = 3_686_400
    if width * height < min_px:
        scale = (min_px / (width * height)) ** 0.5
        width  = ((int(width  * scale) + 7) // 8) * 8
        height = ((int(height * scale) + 7) // 8) * 8

    payload: dict = {
        "model": SEEDREAM_MODEL,
        "prompt": prompt,
        "size": f"{width}x{height}",
        "n": 1,
        "response_format": "url",
        "watermark": False,
        "image": data_urls if len(data_urls) > 1 else data_urls[0],
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        resp = await client.post(
            SEEDREAM_URL,
            json=payload,
            headers={"Authorization": f"Bearer {SEEDREAM_API_KEY}"},
        )
        resp.raise_for_status()
        data = resp.json()

    image_url = data.get("image_url")
    if not image_url:
        items = data.get("data") or []
        if items and isinstance(items[0], dict):
            image_url = items[0].get("url") or items[0].get("image_url")
    if not image_url:
        raise ValueError(f"Seedream returned no image_url. Response: {data}")

    # Download the generated image
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        dl = await client.get(image_url)
        dl.raise_for_status()
        return dl.content, image_url


async def _submit_animate(image_bytes: bytes, video_bytes: bytes, prompt: str, resolution: str = "1080p") -> str:
    """POST to Parrot /animate; return parrot video_id."""
    files = {
        "image": ("image.jpg", image_bytes, "image/jpeg"),
        "video": ("video.mp4", video_bytes, "video/mp4"),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            PARROT_ANIMATE_URL,
            headers={"X-API-KEY": PARROT_API_KEY},
            files=files,
            data={"promptText": prompt, "resolution": resolution},
        )
        resp.raise_for_status()
        result = resp.json()

    vid_id = result.get("video_id") or result.get("id") or result.get("jobId")
    if not vid_id:
        raise ValueError(f"Parrot /animate returned no video_id. Response: {result}")
    return vid_id


async def _poll_until_done(job_id: str, parrot_vid_id: str) -> None:
    """Background task: poll Parrot every 5 s for up to 10 min."""
    timeout, elapsed, interval = 600, 0, 5
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.get(
                    f"{PARROT_POLL_BASE}/videos/{parrot_vid_id}",
                    headers={"X-API-KEY": PARROT_API_KEY},
                )
                resp.raise_for_status()
                result = resp.json()

            status = result.get("status", "").lower()
            video_url = (
                result.get("video_url")
                or result.get("videoUrl")
                or result.get("url")
            )
            logger.info("Poll job=%s parrot_status=%s", job_id, status)
            _jobs[job_id]["parrot_status"] = status

            if status in ("finished", "completed", "done", "success"):
                _jobs[job_id].update(status="completed", video_url=video_url)
                return
            if status in ("failed", "error"):
                _jobs[job_id].update(
                    status="failed",
                    error=result.get("message", "Generation failed"),
                )
                return
        except Exception as exc:
            logger.warning("Poll error job=%s: %s", job_id, exc)

    _jobs[job_id].update(status="failed", error="Timed out after 10 minutes")


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.post(
    "/generate",
    summary="Start a ref-video-to-video job",
    response_description="Returns job_id immediately; poll /status/{job_id}",
)
async def generate(
    prompt: str = Form(
        ...,
        description="Motion / scene description for the generated video",
    ),
    ref_video: UploadFile = File(
        None,
        description="Reference video file (MP4 / MOV / WebM). Either this or ref_video_url is required.",
    ),
    ref_video_url: str = Form(
        None,
        description="Instagram or TikTok URL to use as reference video.",
    ),
    character_images: List[UploadFile] = File(
        ...,
        description="Character identity images — 1 to 3 files (JPG / PNG / WebP)",
    ),
    aspect_ratio: str = Form(
        "9:16",
        description="Output aspect ratio: 9:16 | 16:9 | 1:1",
    ),
    resolution: str = Form(
        "1080p",
        description="Output resolution: 480p | 720p | 1080p",
    ),
    use_seedream: bool = Form(
        True,
        description="If true, run Seedream to generate a pose-matched image first. If false, use the first character_image directly as the pose image.",
    ),
):
    """
    **Full pipeline in one call:**

    1. Extract first frame from `ref_video`
    2. Generate pose-matched image with Seedream
       - reference order: `[char_1, char_2?, char_3?, first_frame]`
    3. Animate the pose image with the ref video via Parrot
    4. Return `job_id` — poll `GET /status/{job_id}` for result
    """
    if not (1 <= len(character_images) <= 3):
        raise HTTPException(400, "Provide 1 to 3 character_images files")
    if ref_video is None and not ref_video_url:
        raise HTTPException(400, "Provide either ref_video file or ref_video_url")

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "processing", "video_url": None, "image_url": None, "error": None}

    try:
        # ── Read uploads ──────────────────────────────────────────────────────
        if ref_video_url:
            ref_bytes, ext, is_image = await _download_from_social_url(ref_video_url)
        else:
            ref_bytes = await ref_video.read()
            ext = os.path.splitext(ref_video.filename or "video.mp4")[1] or ".mp4"
            is_image = _detect_is_image(ref_video.filename or "", ref_video.content_type or "")
        char_bytes_list = [await img.read() for img in character_images]

        char_data_urls = [_to_jpeg_data_url(b) for b in char_bytes_list]
        aspect_map = {"9:16": (1024, 1820), "16:9": (1820, 1024), "1:1": (1024, 1024)}
        pose_w, pose_h = aspect_map.get(aspect_ratio, (1024, 1820))
        n = len(char_data_urls)

        # ── Image i2i mode ────────────────────────────────────────────────────
        if is_image:
            logger.info("job=%s  image input detected — running Seedream i2i only", job_id)
            ref_data_url = _to_jpeg_data_url(ref_bytes)
            reference_images = char_data_urls + [ref_data_url]
            seedream_prompt = (
                f"[Reference Character] Use the character's face, body shape and "
                f"appearance from images 1-{n} for identity consistency. "
                f"[Reference Composition] Follow the exact composition, angle, "
                f"background and lighting from image {len(reference_images)}. "
                f"Generate: {prompt}. "
                f"Keep the character's appearance identical to images 1-{n}."
            )
            logger.info("job=%s  calling Seedream i2i %dx%d", job_id, pose_w, pose_h)
            _, generated_image_url = await _call_seedream(seedream_prompt, reference_images, pose_w, pose_h)
            logger.info("job=%s  Seedream i2i done: %s", job_id, generated_image_url)
            _jobs[job_id].update(status="completed", image_url=generated_image_url)
            return {"job_id": job_id, "status": "completed", "image_url": generated_image_url}

        # ── Video pipeline ────────────────────────────────────────────────────
        video_bytes = ref_bytes

        # ── First frame ───────────────────────────────────────────────────────
        logger.info("job=%s  extracting first frame", job_id)
        frame_bytes = _extract_first_frame(video_bytes, ext)

        # ── data-URLs: [char_1, …, char_n, first_frame] ───────────────────────
        frame_data_url   = _to_jpeg_data_url(frame_bytes)
        reference_images = char_data_urls + [frame_data_url]

        # ── Pose image: Seedream or direct ────────────────────────────────────
        if use_seedream:
            seedream_prompt = (
                f"[Reference Character] Use the character's face, body shape and "
                f"appearance from images 1-{n} for identity consistency. "
                f"[Reference Pose/Composition] Follow the exact pose, camera angle, "
                f"background and lighting from image {len(reference_images)}. "
                f"Generate: {prompt}. "
                f"Keep the character's appearance identical to images 1-{n}."
            )
            logger.info("job=%s  calling Seedream %dx%d with %d refs", job_id, pose_w, pose_h, len(reference_images))
            pose_image_bytes, _ = await _call_seedream(seedream_prompt, reference_images, pose_w, pose_h)
            logger.info("job=%s  pose image ready (%d bytes)", job_id, len(pose_image_bytes))
        else:
            pose_image_bytes = char_bytes_list[0]
            logger.info("job=%s  skipping Seedream, using char_image[0] directly", job_id)

        # ── Animate ───────────────────────────────────────────────────────────
        duration       = _get_duration(video_bytes, ext)
        animate_prompt = _duration_suffix(prompt, duration)

        logger.info("job=%s  submitting animate job (prompt=%r, resolution=%s)", job_id, animate_prompt, resolution)
        parrot_vid_id = await _submit_animate(pose_image_bytes, video_bytes, animate_prompt, resolution)
        _jobs[job_id]["parrot_video_id"] = parrot_vid_id
        logger.info("job=%s  parrot_video_id=%s", job_id, parrot_vid_id)

        asyncio.create_task(_poll_until_done(job_id, parrot_vid_id))
        return {"job_id": job_id, "status": "processing"}

    except Exception as exc:
        logger.error("job=%s  FAILED: %s", job_id, exc)
        _jobs[job_id].update(status="failed", error=str(exc))
        raise HTTPException(500, str(exc))


@app.get(
    "/status/{job_id}",
    summary="Poll job status",
)
async def get_status(job_id: str):
    """
    Returns current status for a job.

    | status       | meaning                          |
    |--------------|----------------------------------|
    | `processing` | Still running                    |
    | `completed`  | Done — `video_url` is set        |
    | `failed`     | Error — check `error` field      |
    """
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "job_id":    job_id,
        "status":    job["status"],
        "video_url": job.get("video_url"),
        "image_url": job.get("image_url"),
        "error":     job.get("error"),
    }


@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True}


@app.get("/debug", include_in_schema=False)
async def debug():
    import shutil
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    path_env = os.environ.get("PATH", "")
    return {
        "ffmpeg": ffmpeg_path,
        "ffprobe": ffprobe_path,
        "PATH": path_env,
    }
