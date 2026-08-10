import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
WORK_DIR = BASE_DIR / "jobs"
WORK_DIR.mkdir(exist_ok=True)

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class PrepareRequest(BaseModel):
    url: str


class ProcessRequest(BaseModel):
    job_id: str
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    text: str = ""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


MAX_PROCESS_WIDTH = 720  # giới hạn chiều rộng khi xử lý filter để tránh tràn RAM
STALE_JOB_SECONDS = 30 * 60  # dọn job cũ quá 30 phút chưa xử lý xong (bị bỏ dở)


def cleanup(folder: Path):
    shutil.rmtree(folder, ignore_errors=True)


def cleanup_stale_jobs():
    if not WORK_DIR.exists():
        return
    now = time.time()
    for entry in WORK_DIR.iterdir():
        try:
            if entry.is_dir() and (now - entry.stat().st_mtime) > STALE_JOB_SECONDS:
                cleanup(entry)
        except FileNotFoundError:
            pass


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def get_video_size(path: Path):
    result = run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json", str(path),
    ], timeout=30)
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    return int(stream["width"]), int(stream["height"])


@app.post("/api/prepare")
def prepare(req: PrepareRequest):
    cleanup_stale_jobs()

    url = req.url.strip()
    if not url or "tiktok.com" not in url:
        raise HTTPException(status_code=400, detail="Link không hợp lệ. Hãy dán link TikTok.")

    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = run([
            "yt-dlp", "-f", "mp4/best",
            "-o", str(job_dir / "raw.%(ext)s"),
            "--no-playlist", url,
        ])
        if result.returncode != 0:
            cleanup(job_dir)
            raise HTTPException(
                status_code=400,
                detail=f"Không tải được video. Kiểm tra lại link. Chi tiết: {result.stderr[-400:]}",
            )

        raw_files = list(job_dir.glob("raw.*"))
        if not raw_files:
            cleanup(job_dir)
            raise HTTPException(status_code=500, detail="Tải video thất bại, không tìm thấy file.")
        raw_file = raw_files[0]

        frame_path = job_dir / "frame.jpg"
        run([
            "ffmpeg", "-y", "-ss", "1", "-i", str(raw_file),
            "-vframes", "1", "-q:v", "3", str(frame_path),
        ], timeout=60)
        if not frame_path.exists():
            run([
                "ffmpeg", "-y", "-i", str(raw_file),
                "-vframes", "1", "-q:v", "3", str(frame_path),
            ], timeout=60)

        width, height = get_video_size(raw_file)

        return {"job_id": job_id, "width": width, "height": height}

    except subprocess.TimeoutExpired:
        cleanup(job_dir)
        raise HTTPException(status_code=504, detail="Xử lý quá lâu, thử lại sau.")
    except HTTPException:
        raise
    except Exception as e:
        cleanup(job_dir)
        raise HTTPException(status_code=500, detail=f"Lỗi không xác định: {str(e)}")


@app.get("/api/frame/{job_id}")
def get_frame(job_id: str):
    frame_path = WORK_DIR / job_id / "frame.jpg"
    if not frame_path.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh xem trước.")
    return FileResponse(str(frame_path), media_type="image/jpeg")


def escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


@app.post("/api/process")
def process(req: ProcessRequest, background_tasks: BackgroundTasks):
    job_dir = WORK_DIR / req.job_id
    raw_files = list(job_dir.glob("raw.*")) if job_dir.exists() else []
    if not raw_files:
        raise HTTPException(status_code=404, detail="Không tìm thấy video, hãy tải lại từ đầu.")
    raw_file = raw_files[0]
    output_file = job_dir / "output.mp4"

    try:
        orig_width, orig_height = get_video_size(raw_file)
        has_region = req.w > 0 and req.h > 0

        if has_region:
            # Thu nhỏ độ phân giải trước khi xử lý filter để tránh tràn RAM
            # trên gói Free (blur/overlay/drawtext + mã hóa lại rất tốn bộ nhớ
            # nếu giữ nguyên độ phân giải gốc, thường 1080x1920 trở lên).
            if orig_width > MAX_PROCESS_WIDTH:
                width = MAX_PROCESS_WIDTH
                height = int(orig_height * (MAX_PROCESS_WIDTH / orig_width))
                height -= height % 2  # ffmpeg cần số chẵn
                scale_step = f"[0:v]scale={width}:{height}[src]"
                src = "src"
            else:
                width, height = orig_width, orig_height
                scale_step = None
                src = "0:v"

            rw = max(10, int(req.w * width))
            rh = max(10, int(req.h * height))
            rx = min(max(0, int(req.x * width)), width - rw)
            ry = min(max(0, int(req.y * height)), height - rh)

            filters = []
            if scale_step:
                filters.append(scale_step)
            filters.append(f"[{src}]crop={rw}:{rh}:{rx}:{ry},boxblur=10:4[blurred]")
            filters.append(f"[{src}][blurred]overlay={rx}:{ry}[bg]")
            last = "bg"

            if req.text.strip():
                fontsize = max(14, rh // 4)
                safe_text = escape_drawtext(req.text.strip())
                ty = ry + (rh // 2) - (fontsize // 2)
                filters.append(
                    f"[{last}]drawtext=fontfile={FONT_PATH}:text='{safe_text}':"
                    f"x=(w-text_w)/2:y={ty}:fontsize={fontsize}:fontcolor=white:"
                    f"box=1:boxcolor=black@0.35:boxborderw=8[out]"
                )
                last = "out"
            else:
                filters.append(f"[{last}]null[out]")
                last = "out"

            filter_complex = ";".join(filters)

            ff_result = run([
                "ffmpeg", "-y", "-i", str(raw_file),
                "-filter_complex", filter_complex,
                "-map", f"[{last}]",
                "-an",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-threads", "1",
                str(output_file),
            ], timeout=180)
        else:
            ff_result = run([
                "ffmpeg", "-y", "-i", str(raw_file),
                "-c:v", "copy", "-an",
                str(output_file),
            ], timeout=120)

        if ff_result.returncode != 0 or not output_file.exists():
            raise HTTPException(
                status_code=500,
                detail=f"Lỗi khi xử lý video: {ff_result.stderr[-400:]}",
            )

        background_tasks.add_task(cleanup, job_dir)

        return FileResponse(
            path=str(output_file),
            media_type="video/mp4",
            filename="tiktok_edited.mp4",
        )

    except subprocess.TimeoutExpired:
        cleanup(job_dir)
        raise HTTPException(status_code=504, detail="Xử lý quá lâu, thử lại sau.")
    except HTTPException:
        raise
    except Exception as e:
        cleanup(job_dir)
        raise HTTPException(status_code=500, detail=f"Lỗi không xác định: {str(e)}")
