import os
import uuid
import shutil
import subprocess
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

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class DownloadRequest(BaseModel):
    url: str


@app.get("/", response_class=HTMLResponse)
def home():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def cleanup(folder: Path):
    try:
        shutil.rmtree(folder, ignore_errors=True)
    except Exception:
        pass


@app.post("/api/download")
def download_video(req: DownloadRequest, background_tasks: BackgroundTasks):
    url = req.url.strip()
    if not url or "tiktok.com" not in url:
        raise HTTPException(status_code=400, detail="Link không hợp lệ. Hãy dán link TikTok.")

    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    raw_path_template = str(job_dir / "raw.%(ext)s")

    try:
        # Bước 1: tải video gốc bằng yt-dlp
        result = subprocess.run(
            [
                "yt-dlp",
                "-f", "mp4/best",
                "-o", raw_path_template,
                "--no-playlist",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            cleanup(job_dir)
            raise HTTPException(
                status_code=400,
                detail=f"Không tải được video. Kiểm tra lại link. Chi tiết: {result.stderr[-500:]}",
            )

        raw_files = list(job_dir.glob("raw.*"))
        if not raw_files:
            cleanup(job_dir)
            raise HTTPException(status_code=500, detail="Tải video thất bại, không tìm thấy file.")

        raw_file = raw_files[0]
        output_file = job_dir / "output_no_audio.mp4"

        # Bước 2: dùng ffmpeg bỏ audio, giữ nguyên chất lượng hình
        ff_result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(raw_file),
                "-c:v", "copy",
                "-an",
                str(output_file),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if ff_result.returncode != 0 or not output_file.exists():
            cleanup(job_dir)
            raise HTTPException(
                status_code=500,
                detail=f"Lỗi khi xử lý bỏ tiếng: {ff_result.stderr[-500:]}",
            )

        # Dọn file gốc, chỉ giữ file kết quả
        raw_file.unlink(missing_ok=True)

        # Xóa thư mục job sau khi trả file xong (chạy nền)
        background_tasks.add_task(cleanup, job_dir)

        return FileResponse(
            path=str(output_file),
            media_type="video/mp4",
            filename="tiktok_no_audio.mp4",
        )

    except subprocess.TimeoutExpired:
        cleanup(job_dir)
        raise HTTPException(status_code=504, detail="Xử lý quá lâu, thử lại sau.")
    except HTTPException:
        raise
    except Exception as e:
        cleanup(job_dir)
        raise HTTPException(status_code=500, detail=f"Lỗi không xác định: {str(e)}")
