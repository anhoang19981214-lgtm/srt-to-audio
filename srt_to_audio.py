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
from pathlib import Path

try:
    from gtts import gTTS
except ImportError:
    print("Lỗi: Thiếu thư viện gTTS. Chạy: pip install gTTS")
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

def text_to_audio_segment(text: str, lang: str = "vi", slow: bool = False) -> AudioSegment:
    """Tạo AudioSegment từ chuỗi text bằng gTTS"""
    buf = io.BytesIO()
    tts = gTTS(text=text, lang=lang, slow=slow)
    tts.write_to_fp(buf)
    buf.seek(0)
    seg = AudioSegment.from_file(buf, format="mp3")
    return seg


def build_audio(entries: list[dict], lang: str = "vi",
                slow: bool = False, speed_factor: float = 1.0) -> AudioSegment:
    """
    Xây dựng track âm thanh cuối cùng.
    Mỗi đoạn TTS được đặt đúng vị trí start_ms của SRT.
    Nếu TTS dài hơn khoảng cho phép, tăng tốc độ để vừa.
    """
    if not entries:
        return AudioSegment.silent(duration=1000)

    last_end = entries[-1]["end_ms"]
    master   = AudioSegment.silent(duration=last_end + 500)

    total = len(entries)
    for i, entry in enumerate(entries):
        print(f"  [{i+1}/{total}] #{entry['index']} | {entry['start_ms']/1000:.2f}s → {entry['end_ms']/1000:.2f}s | {entry['text'][:60]}")

        start_ms  = entry["start_ms"]
        allowed_ms = entry["end_ms"] - start_ms  # khoảng thời gian tối đa

        try:
            seg = text_to_audio_segment(entry["text"], lang=lang, slow=slow)
        except Exception as e:
            print(f"    ⚠ Lỗi TTS: {e}")
            continue

        seg_len = len(seg)

        # Nếu TTS dài hơn allowed, tăng tốc
        if seg_len > allowed_ms and allowed_ms > 0:
            ratio = seg_len / allowed_ms
            # pydub: speedup bằng cách thay đổi frame_rate
            new_rate = int(seg.frame_rate * ratio * speed_factor)
            seg = seg._spawn(seg.raw_data, overrides={"frame_rate": new_rate})
            seg = seg.set_frame_rate(24000)
            print(f"    ↩ Đã tăng tốc x{ratio:.2f}")

        # Overlay đoạn vào đúng vị trí
        master = master.overlay(seg, position=start_ms)

        # Nghỉ ngắn để tránh rate-limit gTTS
        time.sleep(0.3)

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
