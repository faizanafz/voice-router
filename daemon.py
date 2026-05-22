#!/usr/bin/env python3
"""Voice Router Daemon — captures audio once and dispatches transcripts to the MCP server."""

import io
import logging
import os
import pathlib
import re
import socket
import subprocess
import time

import numpy as np
import httpx
import sounddevice as sd
import webrtcvad
from scipy import signal
from scipy.io import wavfile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [daemon] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

ROUTER_DIR = pathlib.Path("/tmp/voice-router")
DISPATCH_SOCKET = ROUTER_DIR / "dispatch.sock"

SAMPLE_RATE = 48000
VAD_SAMPLE_RATE = 16000
VAD_CHUNK_MS = 30
SILENCE_THRESHOLD_MS = 2000
GRACE_MS = 1000
MIN_SPEECH_MS = 300
MAX_DURATION_S = 45.0

WHISPER_URL = os.environ.get("VOICE_ROUTER_WHISPER_URL", "http://127.0.0.1:2022/v1/audio/transcriptions")
WHISPER_LANGUAGE = os.environ.get("VOICE_ROUTER_LANGUAGE", "en")
TTS_PLAYING_FLAG = ROUTER_DIR / "tts-playing"
WAITERS_FLAG = ROUTER_DIR / "active-waiters"
TTS_POST_SILENCE_S = 2.0  # extra silence after TTS before listening (room reverb needs time to die)


def dispatch(transcript: str):
    """Send transcript to MCP server via Unix socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(DISPATCH_SOCKET))
        sock.sendall((transcript + "\n").encode())
        sock.close()
        log.info("Dispatched: %r", transcript)
    except FileNotFoundError:
        log.warning("MCP server not running (no dispatch socket)")
    except Exception as e:
        log.warning("Dispatch failed: %s", e)


_TTS_ABORT = object()  # sentinel: recording cancelled due to TTS playback


def record() -> np.ndarray | None:
    """Record audio with VAD; return numpy int16 array, None if no speech, or _TTS_ABORT."""
    vad = webrtcvad.Vad(3)
    chunk_samples = int(SAMPLE_RATE * VAD_CHUNK_MS / 1000)
    vad_chunk_samples = int(VAD_SAMPLE_RATE * VAD_CHUNK_MS / 1000)

    frames: list[np.ndarray] = []
    speech_started = False
    speech_ms = 0
    silence_ms = 0
    elapsed_ms = 0

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=chunk_samples) as stream:
        log.info("Listening…")
        while elapsed_ms < MAX_DURATION_S * 1000:
            chunk, _ = stream.read(chunk_samples)
            frame = chunk.flatten()
            frames.append(frame)
            elapsed_ms += VAD_CHUNK_MS

            resampled_len = int(len(frame) * VAD_SAMPLE_RATE / SAMPLE_RATE)
            vad_chunk = signal.resample(frame, resampled_len)[:vad_chunk_samples].astype(np.int16)

            try:
                is_speech = vad.is_speech(vad_chunk.tobytes(), VAD_SAMPLE_RATE)
            except Exception:
                is_speech = False

            # Abort recording if TTS starts playing (avoids capturing echo), even during grace
            if TTS_PLAYING_FLAG.exists():
                log.info("TTS started — aborting recording to avoid echo")
                return _TTS_ABORT

            if elapsed_ms < GRACE_MS:
                continue

            if is_speech:
                speech_started = True
                speech_ms += VAD_CHUNK_MS
                silence_ms = 0
            elif speech_started:
                silence_ms += VAD_CHUNK_MS
                if silence_ms >= SILENCE_THRESHOLD_MS:
                    log.info("Silence — stopping")
                    break

    if not speech_started or speech_ms < MIN_SPEECH_MS:
        return None

    return np.concatenate(frames)


def transcribe(audio: np.ndarray) -> str:
    buf = io.BytesIO()
    wavfile.write(buf, SAMPLE_RATE, audio)
    buf.seek(0)
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            WHISPER_URL,
            files={"file": ("audio.wav", buf, "audio/wav")},
            data={"model": "whisper-1", "language": WHISPER_LANGUAGE},
        )
    resp.raise_for_status()
    text = resp.json().get("text", "").strip()
    text = re.sub(r'\[BLANK_AUDIO\]|\[INAUDIBLE\]|\[No audio\]|\[\s*[Ss]ilence\s*\]|>>\s*', '', text).strip()
    return text


_NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

_SESSION_SWITCH_RE = re.compile(
    r'\bsession\s+(?P<n>\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)\b',
    re.IGNORECASE,
)


def parse_session_switch(text: str) -> tuple[int, str] | None:
    """Return (0-based session index, remaining message) if text starts with a session address, else None.

    "Session 0, how are you?" → (0, "how are you?")
    "Session one" → (1, "")
    """
    m = _SESSION_SWITCH_RE.search(text.strip())
    if not m:
        return None
    raw = m.group("n").lower()
    n = _NUMBER_WORDS.get(raw) if raw in _NUMBER_WORDS else int(raw)
    if n < 0:
        log.warning("Session index must be >= 0, got %d — ignoring", n)
        return None
    # remainder = everything after the matched phrase (skip any leading punctuation/spaces)
    remainder = text[m.end():].lstrip(" ,;:").strip()
    return n, remainder


NOTIFY_COOLDOWN_S = 5.0  # suppress "Your turn" for this many seconds after a transcript is dispatched


def main():
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Voice Router Daemon started")
    log.info("Whisper: %s  Language: %s", WHISPER_URL, WHISPER_LANGUAGE)
    log.info("Dispatch socket: %s", DISPATCH_SOCKET)

    last_dispatch_time: float = 0.0

    while True:
        try:
            # Wait for any TTS playback to finish before recording
            while TTS_PLAYING_FLAG.exists():
                time.sleep(0.05)
            if TTS_POST_SILENCE_S > 0:
                time.sleep(TTS_POST_SILENCE_S)

            # Only notify when a session is actively waiting AND enough time has passed since last dispatch
            # (suppresses the "Your turn" flash that appears right after the user just spoke)
            since_dispatch = time.monotonic() - last_dispatch_time
            if WAITERS_FLAG.exists() and since_dispatch >= NOTIFY_COOLDOWN_S:
                subprocess.Popen(
                    ["notify-send", "-u", "low", "-t", "2000", "Voice Router", "Your turn",
                     "-h", "string:x-canonical-private-synchronous:voicerouter"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

            audio = record()
            if audio is _TTS_ABORT:
                # TTS was detected mid-recording; ensure we wait for it to finish
                while TTS_PLAYING_FLAG.exists():
                    time.sleep(0.05)
                time.sleep(TTS_POST_SILENCE_S)
                continue
            if audio is None:
                if WAITERS_FLAG.exists() and since_dispatch >= NOTIFY_COOLDOWN_S:
                    subprocess.Popen(
                        ["notify-send", "-u", "low", "-t", "3000", "Voice Router", "No speech detected"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                continue

            log.info("Transcribing…")
            text = transcribe(audio)
            if not text:
                log.info("Empty transcript, skipping")
                continue

            log.info("Transcript: %r", text)
            switch = parse_session_switch(text)
            if switch is not None:
                # Session switch commands always processed regardless of waiter state
                session_index, remainder = switch
                log.info("Session switch → session %d (0-based), remainder: %r", session_index, remainder)
                subprocess.Popen(
                    ["notify-send", "-u", "low", "-t", "2000", "Voice Router", f"Switching to session {session_index}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                dispatch(f"__SELECT_SESSION:{session_index}")
                if remainder and WAITERS_FLAG.exists():
                    dispatch(remainder)
            elif WAITERS_FLAG.exists():
                # Only dispatch regular transcripts when a session is actively waiting
                last_dispatch_time = time.monotonic()
                dispatch(text)
            else:
                log.info("No active waiter — dropping transcript: %r", text)

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error("Error: %s", e, exc_info=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
