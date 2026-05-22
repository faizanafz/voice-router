# Voice Router — Setup & Usage

Multi-session voice mode for Claude Code. One daemon captures audio; multiple Claude Code sessions share it round-robin.

## Architecture

```
Microphone
    │
    ▼
voice-router-daemon   (records, VAD, transcribes via Whisper)
    │  Unix socket dispatch
    ▼
voice-router-mcp      (MCP server, routes transcripts to sessions)
    │  asyncio queues
    ▼
Claude Code session 1 / session 2 / ...
    │  TTS via Kokoro
    ▼
Speakers
```

## Services

| Service | Unit | What it does |
|---------|------|--------------|
| `voice-router-daemon` | `voice-router-daemon.service` | Records audio, runs VAD, sends to Whisper, dispatches transcript |
| `voice-router-mcp` | `voice-router-mcp.service` | MCP HTTP server on port 8766, holds session queues, plays TTS |

Depends on `voicemode-whisper.service` (port 2022) and `voicemode-kokoro.service` (port 8880).

## Starting

```bash
systemctl --user start voice-router-daemon.service
systemctl --user start voice-router-mcp.service
```

Or just invoke `/voice-router` in any Claude Code session — CLAUDE.md handles the start sequence.

## Session ID

The session ID is derived from the tmux session name:

```bash
SESSION_ID=$(tmux display-message -p '#S' 2>/dev/null | sed 's/^ccd$/0/; s/^ccd-//' || echo "$$")
```

- tmux session `ccd` → ID `0`
- tmux session `ccd-1` → ID `1`
- No tmux → PID

When a session re-registers with a different format (e.g. `"0"` vs `"%0"`), the stale equivalent is auto-removed.

## Voice Commands

| Say | Effect |
|-----|--------|
| `session zero` / `session 0` | Lock audio routing to session 0 |
| `session one` / `session 1` | Lock audio routing to session 1 |
| `session two` ... `session ten` | Works up to ten |

Session switching always works, even while Claude is processing — the daemon routes switch commands regardless of `WAITERS_FLAG`.

## Routing Logic

- **Focused session**: whichever session called `converse()` most recently gets the next transcript.
- **Session lock** (`_session_locked = True`): set by an explicit `session N` voice command; subsequent `converse()` calls don't change focus until the user says another session command.
- **Deferred TTS**: if a session is not focused (lock held by another session), its TTS is queued silently and spoken when focus returns.

## Notification Behavior

| Notification | When it shows |
|---|---|
| `Your turn` | Only when a session is actively waiting (`WAITERS_FLAG` set) AND at least 5 s after the last transcript was dispatched |
| `No speech detected` | Same conditions as above |
| `Switching to session N` | Always, on session-switch voice command |

The `Your turn` notification uses `-h string:x-canonical-private-synchronous:voicerouter` so it replaces the previous instance instead of stacking.

## Echo Prevention

1. `TTS_PLAYING_FLAG` is set at the start of `speak()` — before the Kokoro HTTP request — so the daemon aborts recording immediately when TTS is about to play, not just when `paplay` starts.
2. `TTS_POST_SILENCE_S = 0.8` — extra wait after TTS ends before the mic opens again.
3. On MCP server startup, both `WAITERS_FLAG` and `TTS_PLAYING_FLAG` are cleaned up so stale flags from previous runs can't cause false state.

## Idle Transcript Dropping

While no session is actively waiting (`WAITERS_FLAG` not set):
- Daemon still records (for session-switch detection).
- Non-session-switch transcripts are **dropped** — not dispatched. This prevents junk from queuing while Claude is processing.

## Troubleshooting

**"Your turn" notifications appear while Claude is working**
- The `WAITERS_FLAG` file at `/tmp/voice-router/active-waiters` may be stale from a previous run.
- Fix: `systemctl --user restart voice-router-mcp.service` — the server clears both flags on startup.
- Also ensure all Claude Code sessions re-ran `/voice-router` after the restart.

**Session switch not working (e.g. "session zero" routes to wrong place)**
- Check registered sessions: `session_status()` MCP call.
- A session may be registered with a stale/duplicate ID (e.g. both `"0"` and `"%0"`).
- Fix: restart the target session's voice router — re-registering removes stale equivalents.

**Echo / TTS captured by mic**
- TTS playback abort fires on every loop iteration — the mic should stop as soon as TTS starts.
- If echo still occurs, check if `paplay` is routing through a loopback device.
- `journalctl --user -u voice-router-daemon -n 50` shows abort events.

## File Layout

```
~/Projects/voice-router/
├── daemon.py                  # Audio capture, VAD, Whisper, dispatch
├── mcp_server.py              # MCP tools: register/unregister/converse/status
├── start-daemon.sh            # Launcher for daemon service
├── start-session.sh           # Launcher for MCP service
├── voice-router-daemon.service
├── voice-router-mcp.service
├── CHANGELOG.md
└── SETUP.md                   # This file

/tmp/voice-router/
├── dispatch.sock              # Unix socket: daemon → MCP
├── tts-playing                # Flag: TTS currently playing (paplay running)
└── active-waiters             # Flag: at least one session in queue.get()
```

## Claude Code MCP Config

```bash
claude mcp add --transport http voice-router http://127.0.0.1:8766/mcp
```
