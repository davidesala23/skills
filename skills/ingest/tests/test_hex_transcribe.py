import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("hex_reader", SCRIPTS / "hex_transcribe.py")
HEX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HEX)

FAKE_INTERPRETER = '''import json, os, pathlib, sys
_, program, audio, model, output, language = sys.argv[1:]
assert os.environ["HF_HUB_OFFLINE"] == "1"
assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
audio, model = pathlib.Path(audio), pathlib.Path(model)
if audio.stem == "system" and (model.parent / "fail-system").exists():
    print("synthetic transcription failure", file=sys.stderr)
    sys.exit(7)
with (model.parent / "calls").open("a") as log:
    log.write(audio.stem + "\\n")
result = {"text": "speech from " + audio.stem, "language": json.loads(language) or "en",
          "segments": [{"start": 0.5, "end": 1.5, "text": "speech from " + audio.stem}]}
if (model.parent / "empty-speech").exists():
    result.update({"text": "", "segments": []})
pathlib.Path(output).write_text(json.dumps(result))
'''


class HexTranscriptionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.recording = self.root / "recordings" / "record-1"
        self.recording.mkdir(parents=True)
        self.model = self.root / "model"
        self.model.mkdir()
        self.fake = self.root / "fake-python"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_INTERPRETER)
        self.fake.chmod(0o700)
        self.config = {
            "runtime_dir": str(self.root / "runtime"),
            "hex_recordings_dir": str(self.root / "recordings"),
            "hex_metadata_filename": "manifest.json", "hex_transcript_filename": "transcript.md",
            "hex_microphone_filename": "microphone.wav", "hex_system_filename": "system.wav",
            "hex_whisper_python": str(self.fake), "hex_whisper_model": str(self.model),
            "hex_whisper_language": None, "hex_transcription_timeout_seconds": 5,
        }
        self.manifest = {"schema_version": 1, "id": "record-1", "title": "Stand-up",
                         "status": "complete", "created_at_ms": 1000, "ended_at_ms": 3000,
                         "duration_ms": 2000,
                         "microphone": {"file": "microphone.wav", "first_pts_us": 5_000_000,
                                        "first_arrival_us": 100, "dropped_packets": 0},
                         "system": {"file": "system.wav", "first_pts_us": 6_000_000,
                                    "first_arrival_us": 200, "dropped_packets": 0}}
        self.write_manifest()
        (self.recording / "transcript.md").write_text("# Stand-up\n\n")
        for channel in ("microphone", "system"):
            (self.recording / (channel + ".wav")).write_bytes(b"fixture-audio")

    def write_manifest(self):
        (self.recording / "manifest.json").write_text(json.dumps(self.manifest))

    def source_hashes(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.recording.iterdir() if path.is_file()}

    def test_heading_only_transcript_needs_recovery(self):
        info, _ = HEX.inspect(self.config, "record-1")
        self.assertEqual(info["state"], "needs_transcription")
        self.assertFalse((self.root / "runtime").exists())

    def test_populated_transcript_shortcut_does_not_need_model_or_interpreter(self):
        (self.recording / "transcript.md").write_text("# Stand-up\n\nActual meeting text.\n")
        self.config["hex_whisper_python"] = None
        self.config["hex_whisper_model"] = None
        before = self.source_hashes()
        with patch.object(HEX.subprocess, "run") as run:
            result = HEX.transcribe(self.config, "record-1")
        run.assert_not_called()
        self.assertEqual(result["kind"], "existing")
        self.assertEqual(result["transcript_path"], str(self.recording / "transcript.md"))
        self.assertEqual(self.source_hashes(), before)
        self.assertFalse((self.root / "runtime").exists())

    def test_fake_two_track_transcription_preserves_pts_offsets_and_sources(self):
        before = self.source_hashes()
        result = HEX.transcribe(self.config, "record-1")
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        self.assertEqual(receipt["alignment"]["basis"], "first_pts_us")
        self.assertEqual(receipt["channels"]["system"]["offset_seconds"], 1)
        self.assertEqual([segment["aligned_start"] for segment in receipt["segments"]], [0.5, 1.5])
        self.assertEqual(set(receipt["channels"]), {"microphone", "system"})
        self.assertTrue(all(channel["language"] == "en" for channel in receipt["channels"].values()))
        self.assertEqual(self.source_hashes(), before)

    def test_failed_second_track_retains_first_track_and_source_files(self):
        (self.root / "fail-system").touch()
        before = self.source_hashes()
        with self.assertRaisesRegex(HEX.HexError, "exited 7"):
            HEX.transcribe(self.config, "record-1")
        output = self.root / "runtime/hex/record-1"
        self.assertTrue((output / "microphone.json").is_file())
        self.assertFalse((output / "transcript.json").exists())
        self.assertTrue((output / "system.error.txt").is_file())
        self.assertEqual(self.source_hashes(), before)

    def test_resume_reuses_the_completed_first_track(self):
        failure = self.root / "fail-system"
        failure.touch()
        with self.assertRaises(HEX.HexError):
            HEX.transcribe(self.config, "record-1")
        failure.unlink()
        result = HEX.transcribe(self.config, "record-1")
        self.assertEqual(result["state"], "ready")
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["microphone", "system"])

    def test_missing_local_model_stops_without_starting_a_child(self):
        self.config["hex_whisper_model"] = str(self.root / "absent-model")
        with patch.object(HEX.subprocess, "run") as run:
            with self.assertRaisesRegex(HEX.HexError, "no download"):
                HEX.transcribe(self.config, "record-1")
        run.assert_not_called()

    def test_empty_recognition_is_not_reported_as_a_ready_transcript(self):
        (self.root / "empty-speech").touch()
        with self.assertRaisesRegex(HEX.HexError, "Neither track"):
            HEX.transcribe(self.config, "record-1")
        output = self.root / "runtime/hex/record-1"
        self.assertTrue((output / "microphone.json").is_file())
        self.assertTrue((output / "system.json").is_file())
        self.assertFalse((output / "transcript.json").exists())

    def test_timeout_preserves_source_and_does_not_write_ready_receipt(self):
        before = self.source_hashes()
        with patch.object(HEX.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 5)):
            with self.assertRaisesRegex(HEX.HexError, "timed out"):
                HEX.transcribe(self.config, "record-1")
        self.assertEqual(self.source_hashes(), before)
        self.assertFalse((self.root / "runtime/hex/record-1/transcript.json").exists())

    def test_missing_pts_uses_labelled_approximate_arrival_alignment(self):
        self.manifest["microphone"]["first_pts_us"] = None
        self.manifest["system"]["first_pts_us"] = None
        self.write_manifest()
        timing = HEX.alignment(self.manifest)
        self.assertEqual(timing["basis"], "first_arrival_us")
        self.assertAlmostEqual(timing["offset_seconds"]["system"], 0.0001)

    def test_recording_escape_and_live_recordings_do_not_start_transcription(self):
        with self.assertRaises(HEX.HexError):
            HEX.inspect(self.config, "../model")
        self.manifest["status"] = "recording"
        self.write_manifest()
        with patch.object(HEX.subprocess, "run") as run:
            with self.assertRaisesRegex(HEX.HexError, "incomplete"):
                HEX.transcribe(self.config, "record-1")
        run.assert_not_called()

    def test_missing_configuration_stops_before_reading_sources(self):
        error = io.StringIO()
        with patch.object(HEX, "load_config", side_effect=ValueError("configuration missing")), \
                patch.object(HEX, "inspect") as inspect, contextlib.redirect_stderr(error):
            self.assertEqual(HEX.main(["transcribe", "record-1"]), 2)
        inspect.assert_not_called()
        self.assertEqual(json.loads(error.getvalue())["error"], "hex_transcription_error")


if __name__ == "__main__":
    unittest.main()
