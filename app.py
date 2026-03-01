"""
Flask Web Server cho SRT to Audio Tool
Chạy: python app.py
Truy cập: http://localhost:5000
"""

import os
import re
import io
import time
import uuid
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

try:
    from gtts import gTTS
    from pydub import AudioSegment
except ImportError:
    print("Lỗi: pip install gTTS pydub flask")
    raise

app = Flask(__name__)

UPLOAD_FOLDER = Path("uploads")
OUTPUT_FOLDER = Path("outputs")
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

# Lưu trạng thái xử lý {job_id: {status, progress, message, file}}
jobs: dict[str, dict] = {}


# ─────────────────────────────────────────────
#  SRT Parser
# ─────────────────────────────────────────────

def parse_time(ts: str) -> int:
    ts = ts.strip().replace(".", ",")
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(",")
    return (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(ms)


def parse_srt(content: str) -> list[dict]:
    blocks = re.split(r"\n\s*\n", content.strip())
    entries = []
    for block in blocks:
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0])
        except ValueError:
            continue
        time_match = re.match(
            r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,\.]\d{3})",
            lines[1]
        )
        if not time_match:
            continue
        start_ms = parse_time(time_match.group(1))
        end_ms   = parse_time(time_match.group(2))
        text = " ".join(lines[2:])
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text:
            entries.append({"index": idx, "start_ms": start_ms, "end_ms": end_ms, "text": text})
    return entries


# ─────────────────────────────────────────────
#  Audio Builder (chạy trong thread)
# ─────────────────────────────────────────────

def build_audio_job(job_id: str, srt_content: str, lang: str, slow: bool, fmt: str, filename: str):
    try:
        jobs[job_id]["status"] = "parsing"
        jobs[job_id]["message"] = "Đang phân tích file SRT..."

        entries = parse_srt(srt_content)
        if not entries:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["message"] = "Không tìm thấy đoạn phụ đề nào!"
            return

        total = len(entries)
        last_end = entries[-1]["end_ms"]
        master = AudioSegment.silent(duration=last_end + 500)

        jobs[job_id]["status"] = "processing"
        jobs[job_id]["total"] = total

        for i, entry in enumerate(entries):
            jobs[job_id]["progress"] = i + 1
            jobs[job_id]["message"] = f"Đang tạo giọng đọc đoạn {i+1}/{total}: {entry['text'][:50]}..."

            start_ms   = entry["start_ms"]
            allowed_ms = entry["end_ms"] - start_ms

            try:
                buf = io.BytesIO()
                tts = gTTS(text=entry["text"], lang=lang, slow=slow)
                tts.write_to_fp(buf)
                buf.seek(0)
                seg = AudioSegment.from_file(buf, format="mp3")
            except Exception as e:
                jobs[job_id]["message"] = f"Lỗi TTS đoạn {i+1}: {e}"
                time.sleep(1)
                continue

            if len(seg) > allowed_ms and allowed_ms > 0:
                ratio    = len(seg) / allowed_ms
                new_rate = int(seg.frame_rate * ratio)
                seg = seg._spawn(seg.raw_data, overrides={"frame_rate": new_rate})
                seg = seg.set_frame_rate(24000)

            master = master.overlay(seg, position=start_ms)
            time.sleep(0.25)

        # Xuất file
        jobs[job_id]["status"] = "exporting"
        jobs[job_id]["message"] = "Đang xuất file âm thanh..."

        stem = Path(filename).stem
        out_name = f"{stem}_{job_id[:8]}.{fmt}"
        out_path = OUTPUT_FOLDER / out_name
        master.export(str(out_path), format=fmt)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["message"] = "Hoàn thành!"
        jobs[job_id]["file"]    = out_name
        jobs[job_id]["duration"] = round(len(master) / 1000, 1)

    except Exception as e:
        jobs[job_id]["status"]  = "error"
        jobs[job_id]["message"] = f"Lỗi: {str(e)}"


# ─────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "srt_file" not in request.files:
        return jsonify({"error": "Chưa chọn file"}), 400

    f    = request.files["srt_file"]
    lang = request.form.get("lang", "vi")
    slow = request.form.get("slow", "false") == "true"
    fmt  = request.form.get("format", "mp3")

    if not f.filename.endswith(".srt"):
        return jsonify({"error": "Chỉ hỗ trợ file .srt"}), 400

    content = f.read().decode("utf-8-sig")
    job_id  = str(uuid.uuid4())

    jobs[job_id] = {"status": "queued", "progress": 0, "total": 0, "message": "Đang khởi động...", "file": None}

    t = threading.Thread(target=build_audio_job, args=(job_id, content, lang, slow, fmt, f.filename))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job không tồn tại"}), 404
    return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File chưa sẵn sàng"}), 404
    out_path = OUTPUT_FOLDER / job["file"]
    return send_file(str(out_path), as_attachment=True, download_name=job["file"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "="*50)
    print("  🎙  SRT to Audio - Web Interface")
    print("="*50)
    print(f"  Truy cập: http://localhost:{port}")
    print("="*50 + "\n")
    app.run(debug=False, host="0.0.0.0", port=port)
