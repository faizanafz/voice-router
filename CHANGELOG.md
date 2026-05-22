# Voice Router Changelog

## 2026-05-22

### Fixed
- **"Your turn" notification now fires every recording cycle** — Previously the `notify-send` was guarded by `was_tts`, so it only fired after TTS playback. The first turn and any `skip_tts` turns never showed the notification. Removed the guard so it always fires before the mic opens.
