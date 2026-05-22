#!/usr/bin/env python3
"""Voice Router MCP Server — shared endpoint for multi-session voice routing.

All Claude Code sessions connect to the same port (8766). Each session registers
with a session_id (e.g. tmux pane ID) and gets its own queue. The daemon dispatches
transcripts round-robin based on registration order.

Configure in Claude Code:
    claude mcp add --transport http voice-router http://127.0.0.1:8766/mcp
"""

import asyncio
import logging
import os
import pathlib
import socket
import subprocess
import tempfile

import httpx
from fastmcp import FastMCP

ROUTER_DIR = pathlib.Path("/tmp/voice-router")
SESSIONS_DIR = ROUTER_DIR / "sessions"
DISPATCH_SOCKET = ROUTER_DIR / "dispatch.sock"
MCP_PORT = int(os.environ.get("VOICE_ROUTER_PORT", "8766"))
KOKORO_URL = os.environ.get("VOICE_ROUTER_KOKORO_URL", "http://127.0.0.1:8880/v1/audio/speech")
KOKORO_VOICE = os.environ.get("VOICE_ROUTER_VOICE", "af_sky")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [voice-router] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Shared state — session_id → asyncio.Queue[str]
_sessions: dict[str, asyncio.Queue] = {}
_priority: list[str] = []  # registration order (fallback)
_focused_session: str | None = None  # gets next transcript
_session_locked: bool = False  # True after explicit SELECT_SESSION; converse() won't override focus
_state_lock = asyncio.Lock()
_active_waiters: int = 0  # count of sessions actively blocked in queue.get()

WAITERS_FLAG = ROUTER_DIR / "active-waiters"  # daemon reads this to suppress idle notifications


async def _next_session() -> str | None:
    global _focused_session
    async with _state_lock:
        if not _priority:
            return None
        # Prefer the focused (most recently active) session if it's still registered
        if _focused_session and _focused_session in _priority:
            return _focused_session
        return _priority[0]


async def dispatch_listener():
    """Listen on Unix socket for transcripts from the daemon and route to queues."""
    DISPATCH_SOCKET.unlink(missing_ok=True)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.read(8192)
        transcript = data.decode().strip()
        writer.close()

        if not transcript:
            return

        # Session-switch command from daemon: __SELECT_SESSION:N (1-based index)
        if transcript.startswith("__SELECT_SESSION:"):
            try:
                n = int(transcript.split(":", 1)[1])
            except (ValueError, IndexError):
                log.warning("Bad SELECT_SESSION command: %r", transcript)
                return

            if n < 0:
                log.warning("SELECT_SESSION number must be >= 0, got %d", n)
                return

            async with _state_lock:
                # Match by ccd session number (e.g. "0" for "session 0"), then pane ID ("%0"), then priority index
                str_id = str(n)
                pane_id = f"%{n}"
                if str_id in _sessions:
                    target_id = str_id
                elif pane_id in _sessions:
                    target_id = pane_id
                elif n < len(_priority):
                    target_id = _priority[n]
                else:
                    n_sessions = len(_priority)
                    log.warning("SELECT_SESSION %d not found (%d sessions: %s)", n, n_sessions, _priority)
                    subprocess.Popen(
                        ["notify-send", "-u", "normal", "-t", "3000", "Voice Router",
                         f"Session {n} not found ({n_sessions} registered)"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    return

                global _focused_session, _session_locked
                _focused_session = target_id
                _session_locked = True
                queue = _sessions.get(target_id)

            log.info("Focused session locked to %r (number %d)", target_id, n)
            if queue is not None:
                await queue.put(f"[SELECTED] You are now the active session.")
            return

        target = await _next_session()
        if target is None:
            log.info("No sessions registered — dropping: %r", transcript)
            return

        async with _state_lock:
            queue = _sessions.get(target)

        if queue is None:
            log.warning("Session %r disappeared — dropping transcript", target)
            return

        log.info("→ %s: %r", target, transcript)
        await queue.put(transcript)

    server = await asyncio.start_unix_server(handle, path=str(DISPATCH_SOCKET))
    log.info("Dispatch socket: %s", DISPATCH_SOCKET)
    async with server:
        await server.serve_forever()


TTS_PLAYING_FLAG = ROUTER_DIR / "tts-playing"


async def speak(text: str):
    # Set the flag immediately so the daemon aborts recording during Kokoro fetch (not just during paplay)
    TTS_PLAYING_FLAG.touch()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                KOKORO_URL,
                json={"model": "kokoro", "input": text, "voice": KOKORO_VOICE, "response_format": "wav"},
            )
            resp.raise_for_status()
            wav_bytes = resp.content
    except Exception as e:
        log.warning("TTS failed: %s", e)
        TTS_PLAYING_FLAG.unlink(missing_ok=True)
        return

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _play_wav, wav_bytes)


def _play_wav(wav_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp = f.name
    try:
        subprocess.run(["paplay", tmp], check=False)
    finally:
        TTS_PLAYING_FLAG.unlink(missing_ok=True)
        os.unlink(tmp)


mcp = FastMCP("voice-router")


@mcp.tool()
async def register_session(session_id: str) -> str:
    """Register this Claude Code session for voice routing.

    Call once at the start of a voice session. Use a stable unique ID such as
    the tmux pane ID ($TMUX_PANE) or any short string.

    Args:
        session_id: Unique identifier for this session (e.g. "pane-0", "%3").
    """
    async with _state_lock:
        # Auto-remove equivalent stale sessions (e.g. "0"↔"%0" are the same CCD session under different naming methods)
        equivalents: list[str] = []
        if session_id.isdigit():
            equivalents = [f"%{session_id}"]
        elif session_id.startswith("%") and session_id[1:].isdigit():
            equivalents = [session_id[1:]]
        for stale in equivalents:
            if stale in _sessions:
                log.info("Removing stale equivalent session %r (replaced by %r)", stale, session_id)
                _sessions.pop(stale)
                if stale in _priority:
                    _priority.remove(stale)
                global _focused_session
                if _focused_session == stale:
                    _focused_session = None

        if session_id in _sessions:
            return f"Session {session_id!r} already registered."
        _sessions[session_id] = asyncio.Queue()
        if session_id not in _priority:
            _priority.append(session_id)

    log.info("Registered session %r (total: %d)", session_id, len(_priority))
    return f"Session {session_id!r} registered. Priority position: {_priority.index(session_id) + 1}/{len(_priority)}"


@mcp.tool()
async def unregister_session(session_id: str) -> str:
    """Unregister a session from voice routing."""
    async with _state_lock:
        _sessions.pop(session_id, None)
        if session_id in _priority:
            _priority.remove(session_id)

    log.info("Unregistered session %r", session_id)
    return f"Session {session_id!r} unregistered."


@mcp.tool()
async def converse(
    message: str,
    session_id: str,
    wait_for_response: bool = True,
    skip_tts: bool = False,
    timeout: float = 3600.0,
) -> str:
    """Speak a message and wait for the user's voice response (via voice-router daemon).

    This replaces voicemode's converse() for multi-session use. The daemon captures
    audio once and dispatches transcripts round-robin to registered sessions.

    Args:
        message: Text to speak via TTS.
        session_id: This session's unique ID (must match what was passed to register_session).
        wait_for_response: If False, just speak without listening.
        skip_tts: Skip speaking (just listen).
        timeout: Seconds to wait for a transcript before giving up.
    """
    async with _state_lock:
        queue = _sessions.get(session_id)

    if queue is None:
        return f"Session {session_id!r} not registered. Call register_session first."

    if message and not skip_tts:
        await speak(message)

    if not wait_for_response:
        return "Message spoken."

    # Mark this session as focused so it gets the next transcript (unless locked by SELECT_SESSION)
    global _focused_session, _session_locked
    async with _state_lock:
        if not _session_locked:
            _focused_session = session_id

    global _active_waiters
    _active_waiters += 1
    WAITERS_FLAG.touch()
    try:
        text = await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return f"[No response — timeout after {timeout}s]"
    finally:
        _active_waiters -= 1
        if _active_waiters <= 0:
            _active_waiters = 0
            WAITERS_FLAG.unlink(missing_ok=True)

    return f"Voice response: {text}"


@mcp.tool()
async def session_status() -> str:
    """Show currently registered sessions and priority order."""
    async with _state_lock:
        sessions = list(_priority)
        focused = _focused_session

    if not sessions:
        return "No sessions registered."

    lines = [f"Registered sessions ({len(sessions)}):"]
    for sid in sessions:
        marker = " ← focused (gets next)" if sid == focused else ""
        lines.append(f"  - {sid}{marker}")
    return "\n".join(lines)


def main():
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(exist_ok=True)
    log.info("Voice Router MCP Server starting on port %d", MCP_PORT)

    async def run_all():
        await asyncio.gather(
            dispatch_listener(),
            mcp.run_http_async(transport="streamable-http", host="127.0.0.1", port=MCP_PORT),
        )

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
