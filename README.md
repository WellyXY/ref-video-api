# Ref-Video-to-Video API

Upload a reference video + character images → get back an animated video.

## Workflow

```
ref_video + character_images + prompt
        ↓
Extract first frame (ffmpeg)
        ↓
Seedream: [char1, char2?, char3?, first_frame] → pose-matched image
        ↓
Parrot /animate: pose_image + ref_video → video
        ↓
Poll until done
```

## Endpoints

### `POST /generate`

**Content-Type:** `multipart/form-data`

| Field               | Type   | Required | Description                              |
|---------------------|--------|----------|------------------------------------------|
| `prompt`            | string | ✅        | Motion / scene description               |
| `ref_video`         | file   | ✅        | Reference video (MP4 / MOV / WebM)       |
| `character_images`  | file[] | ✅        | 1–3 character identity images            |
| `aspect_ratio`      | string | —        | `9:16` \| `16:9` \| `1:1` (default `9:16`) |

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

## Example (curl)

```bash
# 1. Start job
curl -X POST https://your-railway-url/generate \
  -F "prompt=A woman dancing gracefully" \
  -F "ref_video=@./dance.mp4" \
  -F "character_images=@./char1.jpg" \
  -F "character_images=@./char2.jpg" \
  -F "aspect_ratio=9:16"

# 2. Poll
curl https://your-railway-url/status/<job_id>
```

## Deploy on Railway

1. Push this repo to GitHub
2. New project on [Railway](https://railway.app) → Deploy from GitHub repo
3. That's it — Railway detects Python + Procfile automatically, nixpacks installs ffmpeg
