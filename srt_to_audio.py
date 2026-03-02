"""
SRT to Audio Tool - Đọc file phụ đề SRT và xuất âm thanh đúng thời gian
Requires: pip install gTTS pydub
ffmpeg phải được cài đặt (https://ffmpeg.org/download.html)
"""

import re
import os
import sys
import argparse
import io
import time
import asyncio
from pathlib import Path

try:
    import edge_tts
except ImportError:
    print("Lỗi: Thiếu thư viện edge-tts. Chạy: pip install edge-tts")
    sys.exit(1)

try:
    from pydub import AudioSegment
    from pydub.generators import Sine
except ImportError:
    print("Lỗi: Thiếu thư viện pydub. Chạy: pip install pydub")
    sys.exit(1)


# ─────────────────────────────────────────────
#  Phân tích file SRT
# ─────────────────────────────────────────────

def parse_time(ts: str) -> int:
    """Chuyển chuỗi SRT timestamp -> mili giây"""
    # Format: HH:MM:SS,mmm
    ts = ts.strip().replace(".", ",")
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(",")
    total = (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(ms)
    return total


def parse_srt(srt_path: str) -> list[dict]:
    """Trả về danh sách {index, start_ms, end_ms, text}"""
    with open(srt_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    blocks = re.split(r"\n\s*\n", content.strip())
    entries = []

    for block in blocks:
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        # Dòng 1: số thứ tự
        try:
            idx = int(lines[0])
        except ValueError:
            continue
        # Dòng 2: timestamp
        time_match = re.match(r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,\.]\d{3})", lines[1])
        if not time_match:
            continue
        start_ms = parse_time(time_match.group(1))
        end_ms   = parse_time(time_match.group(2))
        # Các dòng còn lại là text (bỏ thẻ HTML)
        text = " ".join(lines[2:])
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text:
            entries.append({"index": idx, "start_ms": start_ms, "end_ms": end_ms, "text": text})

    return entries


# ─────────────────────────────────────────────
#  TTS + ghép âm thanh
# ─────────────────────────────────────────────

async def text_to_mp3_bytes(text: str, voice: str = "vi-VN-NamMinhNeural", rate: str = "+0%") -> bytes:
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    audio_data = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data.extend(chunk["data"])
    return bytes(audio_data)

def text_to_audio_segment(text: str, voice: str = "vi-VN-NamMinhNeural", slow: bool = False) -> AudioSegment:
    """Tạo AudioSegment từ chuỗi text bằng edge-tts"""
    rate = "-20%" if slow else "+0%"
    try:
        mp3_bytes = asyncio.run(text_to_mp3_bytes(text, voice, rate))
        buf = io.BytesIO(mp3_bytes)
        seg = AudioSegment.from_file(buf, format="mp3")
        return seg
    except Exception as e:
        print(f"Lỗi TTS nội bộ: {e}")
        raise e


def build_audio(entries: list[dict], lang: str = "vi",
                slow: bool = False) -> AudioSegment:
    """
    Xây dựng track âm thanh cuối cùng.
    Sử dụng giọng Nam Minh (Edge-TTS), tốc độ chuẩn, không nén thời gian!
    Nếu câu dài quá sẽ tự động đẩy câu tiếp theo lùi lại (nối tiếp nhau).
    """
    if not entries:
        return AudioSegment.silent(duration=1000)

    master = AudioSegment.empty()
    voice = "vi-VN-NamMinhNeural" if lang == "vi" else ("en-US-ChristopherNeural" if lang == "en" else "vi-VN-NamMinhNeural")

    total = len(entries)
    for i, entry in enumerate(entries):
        print(f"  [{i+1}/{total}] #{entry['index']} | Target: {entry['start_ms']/1000:.2f}s | {entry['text'][:60]}")

        start_ms  = entry["start_ms"]

        try:
            seg = text_to_audio_segment(entry["text"], voice=voice, slow=slow)
        except Exception as e:
            print(f"    ⚠ Lỗi TTS: {e}")
            continue

        # Đảm bảo vị trí âm thanh không bị phát sớm hơn thời gian của file SRT
        target_start = max(start_ms, len(master))
        if target_start > len(master):
            master += AudioSegment.silent(duration=target_start - len(master))

        # Nối tiếp âm thanh vào cuối
        master += seg

        # Nghỉ ngắn để tránh rate-limit
        time.sleep(0.1)

    return master


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Chuyển file .srt thành file âm thanh đúng thời gian"
    )
    parser.add_argument("srt_file", help="Đường dẫn file .srt")
    parser.add_argument(
        "-o", "--output",
        help="Tên file âm thanh đầu ra (mặc định: tên_srt.mp3)"
    )
    parser.add_argument(
        "-l", "--lang", default="vi",
        help="Ngôn ngữ TTS (mặc định: vi = Tiếng Việt). Ví dụ: en, zh, ja, ko"
    )
    parser.add_argument(
        "--slow", action="store_true",
        help="Đọc chậm hơn"
    )
    parser.add_argument(
        "--format", default="mp3", choices=["mp3", "wav", "ogg"],
        help="Định dạng file đầu ra (mặc định: mp3)"
    )
    args = parser.parse_args()

    srt_path = Path(args.srt_file)
    if not srt_path.exists():
        print(f"Lỗi: Không tìm thấy file '{srt_path}'")
        sys.exit(1)

    output_path = Path(args.output) if args.output else srt_path.with_suffix(f".{args.format}")

    print(f"\n{'='*60}")
    print(f"  SRT to Audio Tool")
    print(f"{'='*60}")
    print(f"  Input : {srt_path}")
    print(f"  Output: {output_path}")
    print(f"  Lang  : {args.lang}")
    print(f"{'='*60}\n")

    # Bước 1: Phân tích SRT
    print("📄 Đang đọc file SRT...")
    entries = parse_srt(str(srt_path))
    if not entries:
        print("Lỗi: Không tìm thấy đoạn phụ đề nào trong file!")
        sys.exit(1)
    print(f"  → Tìm thấy {len(entries)} đoạn phụ đề")
    print(f"  → Tổng thời lượng: {entries[-1]['end_ms']/1000:.1f} giây\n")

    # Bước 2: Sinh âm thanh
    print("🔊 Đang tạo giọng đọc (có thể mất vài phút)...")
    audio = build_audio(entries, lang=args.lang, slow=args.slow)

    # Bước 3: Xuất file
    print(f"\n💾 Đang xuất file '{output_path}'...")
    audio.export(str(output_path), format=args.format)
    print(f"\n✅ Hoàn thành! File đã được lưu: {output_path}")
    print(f"   Thời lượng: {len(audio)/1000:.1f} giây\n")


if __name__ == "__main__":
    main()
