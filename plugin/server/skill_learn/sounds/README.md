# Spoken notifications

Matches the user-memory plugin's voice and mastering:

- Microsoft Edge TTS `en-US-JennyNeural`, normal speech rate.
- 24 kHz, 16-bit mono PCM WAV; approximately -28 LUFS, peaks at or below -10 dBTP.
- Constant attenuation preserves natural pauses and dynamics.
- Recordings ship with the plugin; playback is local, without runtime TTS/network calls.

| File | Spoken text |
|---|---|
| `initiated.wav` | Skill review queued. |
| `started.wav` | Skill review started. |
| `finished.wav` | Skill review completed. |
| `unchanged.wav` | Skill review completed. Nothing to save. |
| `failed.wav` | Skill review failed. |
| `cancelled.wav` | Skill review cancelled. |
