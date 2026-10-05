import math
import shutil
import subprocess
import threading
from pathlib import Path

_PHRASES = {
    "initiated": "Skill review queued.",
    "started": "Skill review started.",
    "finished": "Skill review completed.",
    "unchanged": "Skill review completed. Nothing to save.",
    "failed": "Skill review failed.",
    "cancelled": "Skill review cancelled.",
}


class Notifier:
    def __init__(self, settings, store, player=None):
        self.settings = settings
        self.store = store
        self.player = player or play_sound
        self._playback_lock = threading.Lock()
        self._threads = []

    def play(self, cue, review_id=None):
        if not self.settings.notifications_enabled or self.settings.notification_volume <= 0:
            return
        if not self.settings.notification_types.get(cue, True):
            return
        thread = threading.Thread(target=self._run, args=(cue, review_id), name=f"sound-{cue}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def flush(self):
        for thread in list(self._threads):
            thread.join(2)

    def _run(self, cue, review_id):
        try:
            with self._playback_lock:
                self.player(cue, _PHRASES[cue], self.settings.notification_volume)
        except Exception as error:
            if review_id:
                self.store.add_evidence(review_id, "notification_failure", {"cue": cue, "error": str(error)})


def play_sound(cue, phrase, volume):
    if volume <= 0:
        return
    if cue not in _PHRASES:
        raise ValueError("Unknown notification cue")
    sound = Path(__file__).with_name("sounds") / (cue + ".wav")
    if not sound.is_file():
        raise RuntimeError("Bundled notification recording is missing: " + cue)
    attempts = [
        ["paplay", "--volume=" + str(round(volume * 65536)), str(sound)],
        ["pw-play", "--volume=" + str(volume), str(sound)],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "-volume", str(round(volume * 100)), str(sound)],
        ["canberra-gtk-play", "--volume", str(20 * math.log10(volume)), "-f", str(sound)],
    ]
    if volume == 1:
        attempts.append(["aplay", str(sound)])
    failures = []
    for command in attempts:
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            return
        except (OSError, subprocess.SubprocessError):
            failures.append(command[0])
    raise RuntimeError("No audio player completed notification playback" + (": " + ", ".join(failures) if failures else ""))
