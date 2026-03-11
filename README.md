# Ref-Video-to-Video API

Upload a reference video (or paste an Instagram/TikTok URL) + character images → get back an animated video.

**Base URL:** `https://web-production-de9ee.up.railway.app`

---

## Workflow

**With Seedream (default):**
```
ref_video (file or URL) + character_images + prompt
        ↓
Extract first frame (ffmpeg)
        ↓
Seedream: [char1, char2?, char3?, first_frame] → pose-matched image
        ↓
Parrot /animate: pose_image + ref_video → video
        ↓
Poll until done
```

**Without Seedream (`use_seedream=false`):**
```
ref_video (file or URL) + character_images[0] + prompt
        ↓
Parrot /animate: char_image[0] + ref_video → video
        ↓
Poll until done
```

---

## Endpoints

### `POST /generate`

**Content-Type:** `multipart/form-data`

| Field              | Type   | Required | Description |
|--------------------|--------|----------|-------------|
| `prompt`           | string | ✅        | Motion / scene description |
| `character_images` | file[] | ✅        | 1–3 character identity images (JPG / PNG / WebP) |
| `ref_video`        | file   | ※ either | Reference video file (MP4 / MOV / WebM) |
| `ref_video_url`    | string | ※ either | Instagram or TikTok URL |
| `aspect_ratio`     | string | —        | `9:16` \| `16:9` \| `1:1` (default `9:16`) |
| `resolution`       | string | —        | `480p` \| `720p` \| `1080p` (default `1080p`) |
| `use_seedream`     | bool   | —        | `true` = run Seedream pose generation (default). `false` = skip Seedream, use first character image directly. |

> `ref_video` and `ref_video_url` are mutually exclusive — provide exactly one.

**Response:**
```json
{ "job_id": "uuid", "status": "processing" }
```

---

### `GET /status/{job_id}`

**Response:**
```json
{
  "job_id": "uuid",
  "status": "processing | completed | failed",
  "video_url": "https://...",
  "error": null
}
```

---

## Examples (curl)

**Upload a video file:**
```bash
curl -X POST https://web-production-de9ee.up.railway.app/generate \
  -F "prompt=A woman dancing gracefully" \
  -F "ref_video=@./dance.mp4" \
  -F "character_images=@./char1.jpg" \
  -F "character_images=@./char2.jpg" \
  -F "aspect_ratio=9:16" \
  -F "resolution=1080p"
```

**Use an Instagram URL:**
```bash
curl -X POST https://web-production-de9ee.up.railway.app/generate \
  -F "prompt=A woman dancing gracefully" \
  -F "ref_video_url=https://www.instagram.com/reels/DTcIh7fCQZT/" \
  -F "character_images=@./char1.jpg"
```

**Use a TikTok URL:**
```bash
curl -X POST https://web-production-de9ee.up.railway.app/generate \
  -F "prompt=A woman dancing gracefully" \
  -F "ref_video_url=https://www.tiktok.com/@user/video/7615693631818566935" \
  -F "character_images=@./char1.jpg"
```

**Skip Seedream (direct mode):**
```bash
curl -X POST https://web-production-de9ee.up.railway.app/generate \
  -F "prompt=A woman dancing gracefully" \
  -F "ref_video_url=https://www.instagram.com/reels/DTcIh7fCQZT/" \
  -F "character_images=@./char1.jpg" \
  -F "use_seedream=false"
```

**Poll for result:**
```bash
curl https://web-production-de9ee.up.railway.app/status/<job_id>
```

---

## use_seedream comparison

| | `use_seedream=true` (default) | `use_seedream=false` |
|---|---|---|
| Speed | Slower (extra Seedream API call) | Faster |
| Quality | Character pose aligned to ref video | Uses image as-is |
| Best for | Character needs to match ref motion | Image already has correct pose |

---

## Interactive Docs

```
https://web-production-de9ee.up.railway.app/docs
```
