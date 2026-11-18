"""Cut long silences and standalone filler words, then stitch with a short fade."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.services.caption_rules import is_filler
from app.services.ffmpeg_encode import h264_video_args
from app.services.media_probe import resolve_ffmpeg_binary
from app.services.subprocess_runner import run_subprocess
from app.services.transcription_service import TranscriptWord

_GEUREONIKKA = re.compile(r"^그러니까[,.]?$")
_LETTERS = re.compile(r"[^\w가-힣]")


@dataclass(frozen=True)
class KeepRange:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# Concurrent ffmpeg part encodes. Consumer NVIDIA cards allow 8 NVENC sessions; 4 also suits x264.
PART_WORKERS = 4

def _token(text: str) -> str:
    return _LETTERS.sub("", (text or "").strip())


def is_cut_word(text: str, *, cut_fillers: bool, cut_geureonikka: bool) -> bool:
    token = _token(text)
    if not token:
        return False
    if cut_geureonikka and _GEUREONIKKA.match(token):
        return True
    if cut_fillers and is_filler(token):
        return True
    return bool(cut_fillers and token in {"으", "음음", "으음"})


def keep_ranges(
    words: Iterable[TranscriptWord],
    duration: float,
    *,
    silence_min: float,
    keep_pad: float,
    cut_fillers: bool,
    cut_geureonikka: bool,
) -> list[KeepRange]:
    """Keep spoken spans. Gaps longer than ``silence_min`` shrink to ``keep_pad`` on each side."""
    spoken = [
        word
        for word in words
        if word.text.strip() and not is_cut_word(word.text, cut_fillers=cut_fillers, cut_geureonikka=cut_geureonikka)
    ]
    if not spoken or duration <= 0:
        return [KeepRange(0.0, max(0.0, duration))]
    spoken.sort(key=lambda word: (word.start, word.end))

    ranges: list[KeepRange] = []
    first = spoken[0]
    cursor = 0.0 if first.start <= silence_min else max(0.0, first.start - keep_pad)
    prev_end = first.end
    for word in spoken[1:]:
        gap = word.start - prev_end
        if gap > silence_min:
            ranges.append(KeepRange(cursor, min(duration, prev_end + keep_pad)))
            cursor = max(0.0, word.start - keep_pad)
        prev_end = max(prev_end, word.end)
    trailing = duration - prev_end
    end = duration if trailing <= silence_min else min(duration, prev_end + keep_pad)
    ranges.append(KeepRange(cursor, end))
    return _merge(ranges)


def _merge(ranges: list[KeepRange]) -> list[KeepRange]:
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda item: item.start)
    merged = [ordered[0]]
    for item in ordered[1:]:
        last = merged[-1]
        if item.start <= last.end + 0.01:
            merged[-1] = KeepRange(last.start, max(last.end, item.end))
        else:
            merged.append(item)
    return [item for item in merged if item.duration >= 0.05]


def remap_time(original: float, ranges: list[KeepRange]) -> float:
    elapsed = 0.0
    for item in ranges:
        if original < item.start:
            return elapsed
        if original <= item.end:
            return elapsed + (original - item.start)
        elapsed += item.duration
    return elapsed


def remap_words(
    words: list[TranscriptWord],
    ranges: list[KeepRange],
    *,
    cut_fillers: bool,
    cut_geureonikka: bool,
) -> list[TranscriptWord]:
    remapped: list[TranscriptWord] = []
    for word in words:
        if is_cut_word(word.text, cut_fillers=cut_fillers, cut_geureonikka=cut_geureonikka):
            continue
        start = remap_time(word.start, ranges)
        end = remap_time(word.end, ranges)
        if end <= start:
            continue
        remapped.append(TranscriptWord(start, end, word.text))
    return remapped


def apply_jumpcut(
    settings: Settings,
    source: Path,
    dest: Path,
    ranges: list[KeepRange],
    *,
    xfade: float,
) -> float:
    """Re-encode kept ranges with a short fade so cuts do not click. Returns new duration."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == dest.resolve():
        tmp = dest.with_name(f"{dest.stem}_jumpcut{dest.suffix}")
        duration = apply_jumpcut(settings, source, tmp, ranges, xfade=xfade)
        tmp.replace(dest)
        return duration
    if len(ranges) <= 1 and ranges and ranges[0].start <= 0.02:
        end = ranges[0].end
        if abs(end - _probe_duration(source)) < 0.08:
            if source.resolve() != dest.resolve():
                shutil.copy2(source, dest)
            return end
    work = dest.parent / f"{dest.stem}_parts"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolve_ffmpeg_binary()
    fade = max(0.01, min(xfade, 0.05))
    video_args = h264_video_args(settings)
    parts = [work / f"p{index:04d}.mp4" for index in range(len(ranges))]

    def encode_part(index: int) -> None:
        item = ranges[index]
        fade_out = max(0.0, item.duration - fade)
        run_subprocess(
            [
                ffmpeg, "-y",
                "-ss", f"{item.start:.3f}",
                "-t", f"{item.duration:.3f}",
                "-i", str(source),
                "-vf", "setpts=PTS-STARTPTS",
                "-af", f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={fade_out:.3f}:d={fade:.3f}",
                *video_args,
                "-c:a", "aac", "-b:a", "192k",
                str(parts[index]),
            ],
            timeout=settings.timeout_ffmpeg_seconds,
            stage="longform_jumpcut_part",
            retryable=False,
        )

    # Parts are independent; a 2h source has hundreds of them, each ~0.5s of mostly start-up.
    workers = max(1, min(PART_WORKERS, len(ranges)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(encode_part, range(len(ranges))))
    concat = work / "concat.txt"
    concat.write_text("\n".join(f"file '{part.as_posix()}'" for part in parts), encoding="utf-8")
    run_subprocess(
        [
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-c", "copy",
            str(dest),
        ],
        timeout=settings.timeout_ffmpeg_seconds,
        stage="longform_jumpcut_concat",
        retryable=False,
    )
    shutil.rmtree(work, ignore_errors=True)
    return sum(item.duration for item in ranges)


def _probe_duration(path: Path) -> float:
    from app.services.media_probe import probe_video_file

    probe = probe_video_file(path)
    return float(probe.duration_seconds) if probe is not None else 0.0


def jumpcut_stats(original_duration: float, ranges: list[KeepRange], cut_words: int) -> dict[str, float | int]:
    kept = sum(item.duration for item in ranges)
    return {
        "original_seconds": round(original_duration, 3),
        "kept_seconds": round(kept, 3),
        "removed_seconds": round(max(0.0, original_duration - kept), 3),
        "cuts": max(0, len(ranges) - 1) if ranges else 0,
        "filler_words_removed": cut_words,
    }
