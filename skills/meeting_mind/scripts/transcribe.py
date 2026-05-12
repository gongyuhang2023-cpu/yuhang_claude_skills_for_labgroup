#!/usr/bin/env python3
"""
MeetingMind transcription: WAV → transcript.md with timestamps.
Uses Qwen3-ASR-1.7B (Transformers backend) for local inference.
"""

import argparse
import sys
import time
from pathlib import Path


def load_vocabulary(vocab_path: Path) -> str:
    """Load vocabulary.txt and format as ASR context string."""
    if not vocab_path.exists():
        return ""

    categories: dict[str, list[str]] = {}
    current_category = None

    with open(vocab_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("# MeetingMind") or line.startswith("# 每行") or line.startswith("# 以"):
                continue
            if line.startswith("# "):
                current_category = line[2:].strip()
                categories[current_category] = []
            elif line.startswith("#"):
                continue
            else:
                if current_category is None:
                    current_category = "General"
                    categories[current_category] = []
                categories[current_category].append(line)

    if not categories:
        return ""

    parts = [f"{cat}: {', '.join(terms)}" for cat, terms in categories.items() if terms]
    return "Transcribe accurately. Use these exact spellings for domain terms — " + "; ".join(parts)


def resample_to_16k_mono(input_path: Path) -> Path:
    """Resample audio to 16kHz mono for ASR input."""
    import librosa
    import soundfile as sf

    output_path = input_path.parent / f"{input_path.stem}_16k.wav"

    print(f"  [ASR] Resampling {input_path.name} → 16kHz mono...")
    audio, _ = librosa.load(str(input_path), sr=16000, mono=True)
    sf.write(str(output_path), audio, 16000)

    duration_min = len(audio) / 16000 / 60
    print(f"  [ASR] Duration: {duration_min:.1f} min")
    return output_path


def split_audio(audio_path: Path, chunk_minutes: float = 15.0) -> list[tuple[Path, float]]:
    """Split audio into chunks for long recordings. Returns [(chunk_path, offset_seconds)]."""
    import librosa
    import soundfile as sf

    audio, sr = librosa.load(str(audio_path), sr=16000, mono=True)
    total_seconds = len(audio) / sr
    chunk_samples = int(chunk_minutes * 60 * sr)

    if len(audio) <= chunk_samples:
        return [(audio_path, 0.0)]

    chunks = []
    for i, start in enumerate(range(0, len(audio), chunk_samples)):
        chunk = audio[start:start + chunk_samples]
        chunk_path = audio_path.parent / f"chunk_{i:03d}.wav"
        sf.write(str(chunk_path), chunk, sr)
        offset = start / sr
        chunks.append((chunk_path, offset))

    print(f"  [ASR] Split into {len(chunks)} chunks ({chunk_minutes} min each)")
    return chunks


def transcribe_with_qwen_asr(audio_path: Path, model_name: str, device: str, context: str = "") -> list[dict]:
    """
    Transcribe using qwen-asr package.
    Returns list of segments: [{"start": float, "end": float, "text": str}]
    """
    try:
        from qwen_asr import Qwen3ASRModel
    except ImportError:
        print("[Error] qwen-asr not installed. Run:")
        print("  pip install qwen-asr torch librosa soundfile")
        sys.exit(1)

    print(f"  [ASR] Loading model: {model_name} on {device}...")
    t0 = time.time()
    model = Qwen3ASRModel.from_pretrained(model_name, device_map=device)
    print(f"  [ASR] Model loaded in {time.time() - t0:.1f}s")

    print(f"  [ASR] Transcribing {audio_path.name}...")
    t0 = time.time()
    results = model.transcribe(str(audio_path), context=context)
    elapsed = time.time() - t0
    print(f"  [ASR] Transcription done in {elapsed:.1f}s")

    segments = []
    for r in results:
        text = r.text.strip() if hasattr(r, 'text') else str(r).strip()
        if text:
            segments.append({"start": 0.0, "end": 0.0, "text": text})

    return segments


def format_timestamp(seconds: float) -> str:
    """Format seconds as [HH:MM:SS] or [MM:SS]."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def write_transcript(segments: list[dict], output_path: Path):
    """Write segments to transcript.md."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# 会议转录\n\n")
        for seg in segments:
            start = seg.get("start", 0.0)
            text = seg.get("text", "").strip()
            if text:
                ts = format_timestamp(start)
                f.write(f"{ts} {text}\n")
    print(f"  [ASR] Transcript saved: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="MeetingMind audio transcription")
    parser.add_argument("--audio", type=str, required=True,
                        help="Path to WAV audio file")
    parser.add_argument("--output", type=str, required=True,
                        help="Output directory for transcript")
    parser.add_argument("--model", type=str, default="Qwen/Qwen3-ASR-1.7B",
                        help="Model name/path")
    parser.add_argument("--device", type=str, default="cuda:0",
                        help="Device: cuda:0, cpu, etc.")
    parser.add_argument("--chunk-minutes", type=float, default=15.0,
                        help="Max chunk length in minutes")
    parser.add_argument("--vocabulary", type=str, default="",
                        help="Path to vocabulary.txt hotword file for ASR context")
    args = parser.parse_args()

    audio_path = Path(args.audio)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not audio_path.exists():
        print(f"[Error] Audio file not found: {audio_path}")
        sys.exit(1)

    context = ""
    if args.vocabulary:
        context = load_vocabulary(Path(args.vocabulary))

    print("=" * 60)
    print("  MeetingMind Transcription")
    print("=" * 60)
    print(f"  Audio  : {audio_path}")
    print(f"  Model  : {args.model}")
    print(f"  Device : {args.device}")
    if context:
        term_count = context.count(",") + context.count(";") + 1
        print(f"  Vocab  : {args.vocabulary} ({term_count} terms)")
    print("=" * 60)

    # Step 1: Resample to 16kHz mono
    resampled = resample_to_16k_mono(audio_path)

    # Step 2: Split if needed
    chunks = split_audio(resampled, args.chunk_minutes)

    # Step 3: Transcribe each chunk
    all_segments = []
    for chunk_path, offset in chunks:
        segments = transcribe_with_qwen_asr(chunk_path, args.model, args.device, context=context)
        for seg in segments:
            seg["start"] = seg.get("start", 0.0) + offset
            seg["end"] = seg.get("end", 0.0) + offset
        all_segments.extend(segments)

        # Clean up chunk file if it's a split chunk
        if chunk_path != resampled:
            chunk_path.unlink(missing_ok=True)

    # Clean up resampled file
    if resampled != audio_path:
        resampled.unlink(missing_ok=True)

    # Step 4: Write transcript
    transcript_path = output_dir / "transcript.md"
    write_transcript(all_segments, transcript_path)

    print(f"\n[Done] {len(all_segments)} segments transcribed")
    print(f"  Transcript: {transcript_path}")


if __name__ == "__main__":
    main()
