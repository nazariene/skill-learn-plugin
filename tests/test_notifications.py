import hashlib
import subprocess
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from skill_learn.notify import Notifier, _PHRASES, play_sound
from skill_learn.settings import load_settings


class NotificationTests(unittest.TestCase):
    def test_all_events_have_distinct_playable_bundled_recordings(self):
        import skill_learn.notify

        sounds = Path(skill_learn.notify.__file__).with_name("sounds")
        digests = set()
        for cue in _PHRASES:
            recording = sounds / (cue + ".wav")
            digests.add(hashlib.sha256(recording.read_bytes()).digest())
            with wave.open(str(recording), "rb") as audio:
                self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (24000, 1, 2))
                self.assertGreater(audio.getnframes(), 12000)
        self.assertEqual(len(digests), 6)

    def test_failed_player_falls_back_and_preserves_selected_sound_and_volume(self):
        for failure in (subprocess.CalledProcessError(1, "paplay"), subprocess.TimeoutExpired("paplay", 15)):
            with self.subTest(failure=type(failure).__name__), \
                 patch("skill_learn.notify.shutil.which", side_effect=lambda player: player if player in {"paplay", "pw-play"} else None), \
                 patch("skill_learn.notify.subprocess.run", side_effect=[failure, subprocess.CompletedProcess("pw-play", 0)]) as playback:
                play_sound("finished", _PHRASES["finished"], 0.5)
            attempts = [call.args[0] for call in playback.call_args_list]
            self.assertEqual(attempts[0][:2], ["paplay", "--volume=32768"])
            self.assertEqual(attempts[1][:2], ["pw-play", "--volume=0.5"])
            self.assertEqual(attempts[0][-1], attempts[1][-1])
            self.assertEqual(Path(attempts[1][-1]).name, "finished.wav")

    def test_muting_never_launches_a_player_and_unavailable_players_fail_explicitly(self):
        with patch("skill_learn.notify.subprocess.run") as playback:
            play_sound("finished", _PHRASES["finished"], 0)
            playback.assert_not_called()
        with patch("skill_learn.notify.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "No audio player completed"):
                play_sound("finished", _PHRASES["finished"], 1)

    def test_notifications_default_to_all_cues_and_respect_explicit_muting(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", clear=True):
            home = Path(temporary)
            settings = load_settings(home)
            self.assertTrue(settings.notifications_enabled)
            self.assertEqual(settings.notification_volume, 1)
            self.assertTrue(all(settings.notification_types.values()))
            (home / "settings.yaml").write_text("notifications:\n  types:\n    started: false\n")
            playback = Mock()
            notifier = Notifier(load_settings(home), Mock(), playback)
            notifier.play("started")
            notifier.play("finished")
            notifier.flush()
            self.assertEqual([call.args[0] for call in playback.call_args_list], ["finished"])
            (home / "settings.yaml").write_text("notifications:\n  enabled: false\n")
            disabled = Notifier(load_settings(home), Mock(), playback)
            disabled.play("finished")
            disabled.flush()
            (home / "settings.yaml").write_text("{}\n")
            with patch.dict("os.environ", {"SKILL_LEARNING_NOTIFICATIONS": "0"}):
                muted = Notifier(load_settings(home), Mock(), playback)
                muted.play("finished")
                muted.flush()
            self.assertEqual(playback.call_count, 1)

    def test_notification_dispatch_is_asynchronous_and_speech_does_not_overlap(self):
        started, release, following_started = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def playback(cue, phrase, volume):
            calls.append(cue)
            if cue == "initiated":
                started.set()
                release.wait(2)
            else:
                following_started.set()

        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {"SKILL_LEARNING_NOTIFICATIONS": "1"}):
            notifier = Notifier(load_settings(temporary), Mock(), playback)
            try:
                notifier.play("initiated")
                self.assertTrue(started.wait(1))
                notifier.play("started")
                notifier.play("finished")
                self.assertFalse(following_started.wait(0.1))
                self.assertEqual(calls, ["initiated"])
            finally:
                release.set()
                notifier.flush()
            self.assertCountEqual(calls, ["initiated", "started", "finished"])
