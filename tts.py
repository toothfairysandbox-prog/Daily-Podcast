"""Text-to-speech for the daily brief.

Primary engine is edge-tts: genuinely free, no API key, no quota, and the
neural voices sound like a real person. If Microsoft's endpoint ever refuses
a GitHub runner, we fall back to gTTS so the show still ships.

Set a paid engine later by exporting TTS_ENGINE=openai plus OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

DEFAULT_VOICE = os.getenv("TTS_VOICE", "en-US-AndrewMultilingualNeural")
DEFAULT_RATE = os.getenv("TTS_RATE", "+4%")

# A few voices worth trying -- swap via the TTS_VOICE env var / repo variable:
#   en-US-AndrewMultilingualNeural  warm, conversational male  (default)
#   en-US-BrianMultilingualNeural   crisp newsreader male
#   en-US-AvaMultilingualNeural     warm, conversational female
#   en-US-EmmaMultilingualNeural    bright, upbeat female
#   en-GB-RyanNeural                British male


def _strip_markdown(text: str) -> str:
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                # bold
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)       # italics
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)  # bullets
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)             # links
    text = text.replace("&", " and ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_for_speech(text: str) -> str:
    """Strip anything a narrator would awkwardly read out loud.

    Removing fenced code blocks is right for a stray snippet and catastrophic
    if the whole script happens to be fenced -- that path silently produced an
    empty string and a failed build. So the fence pass only stands if it left
    something behind.
    """
    defenced = _strip_markdown(re.sub(r"```.*?```", " ", text, flags=re.DOTALL))
    if defenced:
        return defenced

    # Everything was inside a fence. Peel the fence off instead of obliterating
    # the text, and narrate what's inside.
    return _strip_markdown(re.sub(r"```(?:\w+)?", " ", text))


async def _edge_tts(text: str, out_path: Path, voice: str, rate: str) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate)
    # A stalled websocket mid-stream is not an error and never returns, so
    # bound it explicitly rather than letting the job hang.
    await asyncio.wait_for(communicate.save(str(out_path)), timeout=240)


def _gtts(text: str, out_path: Path) -> None:
    from gtts import gTTS  # type: ignore

    gTTS(text=text, lang="en", tld="com").save(str(out_path))


def _openai_tts(text: str, out_path: Path) -> None:
    from openai import OpenAI  # type: ignore

    client = OpenAI()
    voice = os.getenv("OPENAI_TTS_VOICE", "onyx")
    model = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    with client.audio.speech.with_streaming_response.create(
        model=model, voice=voice, input=text
    ) as response:
        response.stream_to_file(str(out_path))


def _compress(path: Path) -> None:
    """Re-encode to small, even-loudness mono speech audio.

    A 7-minute brief lands around 1.5 MB, which keeps the repo tiny and makes
    the episode load instantly on a phone.
    """
    if not shutil.which("ffmpeg"):
        print("  ! ffmpeg not found, shipping raw TTS output")
        return

    tmp = path.with_suffix(".tmp.mp3")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(path),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ac", "1", "-ar", "24000", "-b:a", "32k",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True)
        tmp.replace(path)
    except subprocess.CalledProcessError as exc:
        print(f"  ! ffmpeg pass failed ({exc}); keeping raw audio")
        tmp.unlink(missing_ok=True)


def synthesize(text: str, out_path: Path) -> Path:
    """Turn the episode script into an MP3. Raises only if every engine fails."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    spoken = clean_for_speech(text)
    if len(spoken.split()) < 20:
        raise RuntimeError(
            f"refusing to synthesize {len(spoken.split())} words of speech "
            f"(input was {len(text)} chars) -- the script never made it here intact"
        )
    engine = os.getenv("TTS_ENGINE", "edge").lower()

    attempts: list[tuple[str, object]] = []
    if engine == "openai":
        attempts.append(("openai", lambda: _openai_tts(spoken, out_path)))

    # Microsoft's endpoint hands out occasional 403s. They are almost always
    # transient, so try a few times with backoff before giving up on it.
    for i in range(3):
        attempts.append(
            (f"edge-tts (try {i + 1}/3)", lambda i=i: (
                time.sleep(5 * i),
                asyncio.run(_edge_tts(spoken, out_path, DEFAULT_VOICE, DEFAULT_RATE)),
            ))
        )

    attempts.append(("gTTS (fallback voice)", lambda: _gtts(spoken, out_path)))

    last_error: Exception | None = None
    for name, fn in attempts:
        try:
            print(f"  synthesizing with {name} ...")
            fn()
            if out_path.exists() and out_path.stat().st_size > 10_000:
                _compress(out_path)
                size_mb = out_path.stat().st_size / 1_048_576
                print(f"  + audio written: {out_path.name} ({size_mb:.2f} MB)")
                return out_path
            raise RuntimeError("engine produced an empty or truncated file")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {name} failed: {exc.__class__.__name__}: {exc}")
            last_error = exc
            out_path.unlink(missing_ok=True)

    raise RuntimeError(f"all TTS engines failed; last error: {last_error}")


def duration_exact(path: Path) -> float:
    """Length of the finished MP3 in seconds, unrounded."""
    try:
        from mutagen.mp3 import MP3

        return float(MP3(str(path)).info.length)
    except Exception:
        pass
    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, check=True,
            )
            return float(out.stdout.strip())
        except Exception:
            pass
    return 0.0


def duration_seconds(path: Path) -> int:
    """Length of the finished MP3, for the player UI and the RSS feed."""
    return int(duration_exact(path))


def fit_duration(path: Path, limit_seconds: float, max_tempo: float = 1.15) -> float:
    """Make sure a finished cut is no longer than its time limit.

    The show promises a brief of at most three minutes, and for its first
    three days it broke that promise: scripts were sized for an assumed 165
    words a minute, the voice actually reads at about 152, and the model
    drifts over its word limit besides. Four "45-second" cuts ran 3:27.

    A word count is a request; this is the guarantee. If the audio came out
    over its limit, it is sped up just enough to fit -- pitch preserved, and
    capped at max_tempo so it never sounds rushed. Up to about 1.15x is hard
    to notice in speech. A cut that would need more than that is left at the
    cap and reported, because that means the script itself is too long.

    Returns the final duration in seconds.
    """
    actual = duration_exact(path)
    # A few hundredths over is encoder padding, not a long script, and is not
    # worth a second lossy encode.
    if actual <= 0 or actual <= limit_seconds + 0.3:
        return actual
    if not shutil.which("ffmpeg"):
        print(f"  ! {path.name} is {actual:.0f}s (limit {limit_seconds:.0f}s) "
              f"and ffmpeg is missing, so it cannot be fitted")
        return actual

    # A hair past the exact ratio, so rounding in the encoder cannot leave
    # the result a few milliseconds over.
    wanted = actual / limit_seconds * 1.004
    tempo = min(wanted, max_tempo)
    tmp = path.with_suffix(".fit.mp3")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(path),
        "-af", f"atempo={tempo:.4f}",
        "-ac", "1", "-ar", "24000", "-b:a", "32k",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True)
        tmp.replace(path)
    except subprocess.CalledProcessError as exc:
        print(f"  ! could not fit {path.name} ({exc}); keeping it at {actual:.0f}s")
        tmp.unlink(missing_ok=True)
        return actual

    final = duration_exact(path)
    if wanted > max_tempo:
        print(f"  ! {path.name}: {actual:.0f}s needed {wanted:.2f}x to fit "
              f"{limit_seconds:.0f}s; capped at {max_tempo:.2f}x, so it runs "
              f"{final:.0f}s. The script is too long -- tighten its word limit.")
    else:
        print(f"  + fitted {path.name}: {actual:.1f}s -> {final:.1f}s at {tempo:.2f}x")
    return final
