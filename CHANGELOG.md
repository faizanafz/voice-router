# Voice Router Changelog

## 2026-05-22

### Added
- **Voice session switching** — Say "session one", "session two", etc. (or "session 1/2/3") to lock all future audio to that session. The daemon intercepts the pattern and sends a routing command; the selected session receives a `[SELECTED]` notification. Works up to session ten.



### Fixed
- **"Your turn" notification now fires every recording cycle** — Previously the `notify-send` was guarded by `was_tts`, so it only fired after TTS playback. The first turn and any `skip_tts` turns never showed the notification. Removed the guard so it always fires before the mic opens.
