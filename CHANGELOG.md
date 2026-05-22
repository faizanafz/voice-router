# Voice Router Changelog

## 2026-05-22

### Added
- **Voice session switching** — Say "session one", "session two", etc. (or "session 1/2/3") to lock all future audio to that session. The daemon intercepts the pattern and sends a routing command; the selected session receives a `[SELECTED]` notification. Works up to session ten.

### Fixed
- **"Your turn" notification now fires every recording cycle** — Previously the `notify-send` was guarded by `was_tts`, so it only fired after TTS playback. The first turn and any `skip_tts` turns never showed the notification. Removed the guard so it always fires before the mic opens.
- **Notification stacking** — Added `-h string:x-canonical-private-synchronous:voicerouter` so each "Your turn" notification replaces the previous instead of stacking.
- **TTS echo captured by mic** — `TTS_PLAYING_FLAG` is now set at the start of `speak()` (before the Kokoro HTTP request) rather than just before `paplay`. Closes the race where the daemon started recording while TTS was still being fetched. Also increased `TTS_POST_SILENCE_S` from 0.8 to 2.0 s to let room reverb die down.
- **Notifications spamming while Claude processes** — "Your turn" and "No speech detected" are now suppressed when no session is actively waiting (tracked via `WAITERS_FLAG` file). A 5 s cooldown after a transcript is dispatched further prevents the notification flashing immediately after the user finishes speaking.
- **Junk transcripts queuing during processing** — Daemon now drops non-session-switch transcripts when no waiter is active, preventing stale inputs from piling up in queues.
- **Session switch routing broken by duplicate registrations** — When a session re-registers with a different format (e.g. `"0"` vs `"%0"`), the old equivalent session is now automatically removed, keeping routing unambiguous.
- **`[No audio]` transcript not filtered** — Added `[No audio]` to Whisper output stripping regex alongside `[BLANK_AUDIO]`, `[INAUDIBLE]`, etc.
