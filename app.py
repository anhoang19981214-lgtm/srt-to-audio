"""
Flask Web Server cho SRT to Audio Tool
Phiên bản không cần ffmpeg - dùng pydub với gTTS stream trực tiếp
"""

import os
import re
import io
import time
import uuid
import struct
import threading
import asyncio
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

try:
    import edge_tts
except ImportError:
    print("Lỗi: pip install edge-tts flask")
    raise

try:
    import pydub
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False

app = Flask(__name__)

UPLOAD_FOLDER = Path("uploads")
OUTPUT_FOLDER = Path("outputs")
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

jobs: dict = {}


# ─────────────────────────────────────────────
#  SRT Parser
# ─────────────────────────────────────────────

def parse_time(ts: str) -> int:
    ts = ts.strip().replace(".", ",")
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(",")
    return (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(ms)


def parse_srt(content: str) -> list:
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
#  Audio builder — WAV thuần Python (không cần ffmpeg)
# ─────────────────────────────────────────────

def make_silence_wav(duration_ms: int, sample_rate: int = 22050) -> bytes:
    """Tạo dữ liệu WAV im lặng"""
    num_samples = int(sample_rate * duration_ms / 1000)
    pcm_data    = b'\x00\x00' * num_samples  # 16-bit stereo silent
    num_channels   = 1
    bits_per_sample = 16
    byte_rate      = sample_rate * num_channels * bits_per_sample // 8
    block_align    = num_channels * bits_per_sample // 8
    data_size      = len(pcm_data)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, num_channels,
        sample_rate, byte_rate, block_align, bits_per_sample,
        b'data', data_size
    )
    return header + pcm_data


def overlay_wav(base: bytearray, overlay_data: bytes, position_ms: int, sample_rate: int = 22050) -> bytearray:
    """Overlay WAV data vào vị trí offset"""
    header_size = 44
    bytes_per_ms = sample_rate * 2 // 1000  # 16-bit mono
    offset = header_size + int(position_ms * bytes_per_ms)

    overlay_pcm = overlay_data[header_size:]
    end = offset + len(overlay_pcm)

    if end > len(base):
        base.extend(b'\x00' * (end - len(base)))

    for i, b in enumerate(overlay_pcm):
        pos = offset + i
        if pos < len(base):
            # Simple mix: clamp to avoid overflow
            val = int.from_bytes([base[pos]], 'little', signed=True)
            new_val = int.from_bytes([b], 'little', signed=True)
            mixed = max(-32768, min(32767, val + new_val))
            base[pos] = mixed & 0xFF

    return base


def mp3_to_wav_bytes(mp3_bytes: bytes) -> bytes:
    """Chuyển MP3 bytes → WAV bytes dùng pydub (nếu có ffmpeg) hoặc ghi mp3 tạm"""
    if PYDUB_AVAILABLE:
        try:
            seg = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
            seg = seg.set_channels(1).set_frame_rate(22050).set_sample_width(2)
            buf = io.BytesIO()
            seg.export(buf, format="wav")
            return buf.getvalue()
        except Exception:
            pass

    # Fallback: trả về mp3 bytes thô (sẽ lưu dưới dạng mp3 riêng lẻ)
    return None


async def text_to_mp3_bytes(text: str, voice: str, rate: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    audio_data = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data.extend(chunk["data"])
    return bytes(audio_data)

def get_tts_bytes(text: str, lang: str, slow: bool) -> bytes:
    voice = "vi-VN-NamMinhNeural" if lang == "vi" else ("en-US-ChristopherNeural" if lang == "en" else "vi-VN-NamMinhNeural")
    rate = "-20%" if slow else "+0%"
    return asyncio.run(text_to_mp3_bytes(text, voice, rate))


# ─────────────────────────────────────────────
#  Job processor
# ─────────────────────────────────────────────

def build_audio_job(job_id: str, srt_content: str, lang: str, slow: bool, filename: str):
    try:
        jobs[job_id]["status"]  = "parsing"
        jobs[job_id]["message"] = "Đang phân tích file SRT..."

        entries = parse_srt(srt_content)
        if not entries:
            jobs[job_id]["status"]  = "error"
            jobs[job_id]["message"] = "Không tìm thấy đoạn phụ đề nào!"
            return

        total    = len(entries)
        last_end = entries[-1]["end_ms"]

        jobs[job_id]["status"] = "processing"
        jobs[job_id]["total"]  = total

        # ── Strategy: collect all MP3, lưu thành từng file, ghép bằng pydub nếu có ──
        mp3_segments = []

        for i, entry in enumerate(entries):
            jobs[job_id]["progress"] = i + 1
            jobs[job_id]["message"]  = f"Tạo giọng đọc đoạn {i+1}/{total}: {entry['text'][:50]}..."

            try:
                mp3_data = get_tts_bytes(entry["text"], lang, slow)
                mp3_segments.append({
                    "start_ms":  entry["start_ms"],
                    "end_ms":    entry["end_ms"],
                    "mp3_data":  mp3_data,
                })
            except Exception as e:
                jobs[job_id]["message"] = f"Lỗi TTS đoạn {i+1}: {e}"
                time.sleep(1)
                continue

            time.sleep(0.25)

        # ── Ghép âm thanh ──
        jobs[job_id]["status"]  = "exporting"
        jobs[job_id]["message"] = "Đang ghép và xuất file âm thanh..."

        stem     = Path(filename).stem
        out_name = f"{stem}_{job_id[:8]}.mp3"
        out_path = OUTPUT_FOLDER / out_name

        if PYDUB_AVAILABLE:
            # Dùng pydub nếu ffmpeg có trên server
            try:
                master = AudioSegment.empty()
                for seg_info in mp3_segments:
                    seg = AudioSegment.from_file(io.BytesIO(seg_info["mp3_data"]), format="mp3")
                    start_ms   = seg_info["start_ms"]
                    
                    target_start = max(start_ms, len(master))
                    if target_start > len(master):
                        master += AudioSegment.silent(duration=target_start - len(master))

                    master += seg

                master.export(str(out_path), format="mp3")
                duration = round(len(master) / 1000, 1)

            except Exception:
                # ffmpeg không có → xuất từng file MP3 riêng lẻ thành zip
                import zipfile
                out_name = f"{stem}_{job_id[:8]}.zip"
                out_path = OUTPUT_FOLDER / out_name
                with zipfile.ZipFile(str(out_path), 'w') as zf:
                    for idx2, seg_info in enumerate(mp3_segments):
                        zf.writestr(f"segment_{idx2+1:03d}_{seg_info['start_ms']}ms.mp3", seg_info["mp3_data"])
                duration = round(last_end / 1000, 1)
        else:
            # Không có pydub → zip các đoạn MP3
            import zipfile
            out_name = f"{stem}_{job_id[:8]}.zip"
            out_path = OUTPUT_FOLDER / out_name
            with zipfile.ZipFile(str(out_path), 'w') as zf:
                for idx2, seg_info in enumerate(mp3_segments):
                    zf.writestr(f"segment_{idx2+1:03d}_{seg_info['start_ms']}ms.mp3", seg_info["mp3_data"])
            duration = round(last_end / 1000, 1)

        jobs[job_id]["status"]   = "done"
        jobs[job_id]["message"]  = "Hoàn thành!"
        jobs[job_id]["file"]     = out_name
        jobs[job_id]["duration"] = duration
        jobs[job_id]["fmt"]      = "zip" if out_name.endswith(".zip") else "mp3"

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

    if not f.filename.endswith(".srt"):
        return jsonify({"error": "Chỉ hỗ trợ file .srt"}), 400

    content = f.read().decode("utf-8-sig")
    job_id  = str(uuid.uuid4())

    jobs[job_id] = {
        "status": "queued", "progress": 0, "total": 0,
        "message": "Đang khởi động...", "file": None, "fmt": "mp3"
    }

    t = threading.Thread(
        target=build_audio_job,
        args=(job_id, content, lang, slow, f.filename)
    )
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
    print(f"\n{'='*50}")
    print("  🎙  SRT to Audio - Web Interface")
    print(f"{'='*50}")
    print(f"  Truy cập: http://localhost:{port}")
    print(f"{'='*50}\n")
    app.run(debug=False, host="0.0.0.0", port=port)
