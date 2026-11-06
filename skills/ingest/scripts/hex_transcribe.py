#!/usr/bin/env python3
"""Inspect local Hex recordings and recover missing text with a local model.

Commands: list; transcribe <recording-id>. Reads the configured recording root and
schema-v1 manifests. Generated artifacts stay under the configured runtime_dir.
Channel labels describe capture provenance, not verified speaker identities.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from ingest_config import load_config


class HexError(ValueError):
    pass


WHISPER_PROGRAM = """import json, sys
import mlx_whisper
audio, model, output, language = sys.argv[1:]
options = {"path_or_hf_repo": model, "condition_on_previous_text": False,
           "verbose": False}
language = json.loads(language)
if language is not None:
    options["language"] = language
result = mlx_whisper.transcribe(audio, **options)
with open(output, "w", encoding="utf-8") as handle:
    json.dump(result, handle, ensure_ascii=False)
"""


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise HexError("A recording or runtime JSON file is unavailable or invalid: " + str(path)) from None


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def filename(config, key):
    value = config.get(key)
    if not isinstance(value, str) or not value or Path(value).name != value or value in (".", ".."):
        raise HexError("Configure a plain filename for " + key)
    return value


def configured_path(config, key):
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HexError("Configure a filesystem path for " + key)
    return Path(value).expanduser().resolve()


def recording_path(config, identifier):
    root = configured_path(config, "hex_recordings_dir")
    directory = (root / identifier).resolve()
    if directory.parent != root or directory.name != identifier or not directory.is_dir():
        raise HexError("Recording ID must identify an existing direct child of the configured root")
    return directory


def contains_transcript(text):
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    return any(line.strip() and not line.lstrip().startswith("#")
               and line.strip() not in ("---", "***") for line in text.splitlines())


def inspect(config, identifier):
    directory = recording_path(config, identifier)
    manifest_path = directory / filename(config, "hex_metadata_filename")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise HexError("Unsupported Hex manifest schema")
    if manifest.get("id") != identifier:
        raise HexError("Manifest identity does not match the recording directory")
    result = {"id": identifier, "title": manifest.get("title"),
              "created_at_ms": manifest.get("created_at_ms"),
              "ended_at_ms": manifest.get("ended_at_ms"),
              "duration_ms": manifest.get("duration_ms"),
              "manifest_path": str(manifest_path), "state": "incomplete"}
    if manifest.get("status") != "complete":
        return result, manifest
    transcript = directory / filename(config, "hex_transcript_filename")
    if transcript.is_file():
        text = transcript.read_text(encoding="utf-8")
        if contains_transcript(text):
            result.update({"state": "ready", "kind": "existing",
                           "transcript_path": str(transcript),
                           "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
                           "coverage": "native transcript text; audio completeness and speakers unverified"})
            return result, manifest
    tracks = {}
    for channel in ("microphone", "system"):
        name = filename(config, "hex_" + channel + "_filename")
        track = manifest.get(channel)
        if not isinstance(track, dict) or track.get("file") != name:
            raise HexError("Configured audio filename does not match the manifest for " + channel)
        audio = (directory / name).resolve()
        if audio.parent != directory or not audio.is_file() or not audio.stat().st_size:
            result.update({"state": "missing_audio", "missing_channel": channel})
            return result, manifest
        tracks[channel] = str(audio)
    result.update({"state": "needs_transcription", "tracks": tracks})
    return result, manifest


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def alignment(manifest):
    for field in ("first_pts_us", "first_arrival_us"):
        values = {channel: manifest[channel].get(field) for channel in ("microphone", "system")}
        if all(number(value) for value in values.values()):
            origin = min(values.values())
            return {"basis": field, "origin_us": origin,
                    "offset_seconds": {channel: (value - origin) / 1_000_000
                                       for channel, value in values.items()}}
    return {"basis": "unavailable", "origin_us": None,
            "offset_seconds": {"microphone": None, "system": None}}


def validate_result(result):
    if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not isinstance(result.get("segments"), list):
        raise HexError("Transcriber returned no valid text/segments result")
    for segment in result["segments"]:
        if (not isinstance(segment, dict) or not isinstance(segment.get("text"), str)
                or not number(segment.get("start")) or not number(segment.get("end"))
                or not 0 <= segment["start"] <= segment["end"]):
            raise HexError("Transcriber returned an invalid segment")
    return result


def audio_signature(audio):
    stat = audio.stat()
    return {"path": str(audio), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def transcribe_track(config, audio, model, output, deadline):
    signature = {"audio": audio_signature(audio), "model": str(model),
                 "model_files": sorted([p.name, p.stat().st_size, p.stat().st_mtime_ns]
                                       for p in model.iterdir() if p.is_file()),
                 "language": config.get("hex_whisper_language")}
    if output.is_file():
        cached = read_json(output)
        if not isinstance(cached, dict):
            raise HexError("Cached transcription is not a JSON object: " + str(output))
        if cached.get("signature") == signature:
            return validate_result(cached.get("result"))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HexError("The configured transcription deadline expired")
    descriptor, temporary = tempfile.mkstemp(dir=output.parent, prefix=".transcribing-", suffix=".json")
    os.close(descriptor)
    try:
        command = [config["hex_whisper_python"], "-c", WHISPER_PROGRAM, str(audio),
                   str(model), temporary, json.dumps(config.get("hex_whisper_language"))]
        environment = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        try:
            process = subprocess.run(command, capture_output=True, text=True,
                                     timeout=remaining, env=environment)
        except subprocess.TimeoutExpired:
            raise HexError("Transcription timed out; completed track outputs remain in " + str(output.parent)) from None
        if process.returncode:
            error_path = output.with_suffix(".error.txt")
            atomic_write(error_path, process.stderr + "\n" + process.stdout)
            raise HexError("Transcriber exited " + str(process.returncode) + "; details: " + str(error_path))
        result = validate_result(read_json(Path(temporary)))
        write_json(output, {"signature": signature, "result": result})
        return result
    finally:
        Path(temporary).unlink(missing_ok=True)


def transcribe(config, identifier):
    info, manifest = inspect(config, identifier)
    if info["state"] == "ready":
        return info
    if info["state"] != "needs_transcription":
        raise HexError("Recording is not ready for transcription: " + info["state"])
    model = configured_path(config, "hex_whisper_model")
    interpreter = configured_path(config, "hex_whisper_python")
    if not model.is_dir() or not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise HexError("Configure an existing local model directory and executable interpreter; no download is attempted")
    timeout = config.get("hex_transcription_timeout_seconds")
    if not number(timeout) or timeout <= 0:
        raise HexError("hex_transcription_timeout_seconds must be positive")
    root = configured_path(config, "hex_recordings_dir")
    runtime = configured_path(config, "runtime_dir")
    if runtime == root or root in runtime.parents:
        raise HexError("runtime_dir must be outside the source recording root")
    output = runtime / "hex" / identifier
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / ".transcribe.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HexError("A transcription already owns this recording's runtime directory") from None
        deadline = time.monotonic() + timeout
        timing = alignment(manifest)
        tracks, segments = {}, []
        for channel, audio in info["tracks"].items():
            result_path = output / (channel + ".json")
            result = transcribe_track(config, Path(audio), model, result_path, deadline)
            tracks[channel] = {"audio_path": audio, "result_path": str(result_path),
                               "language": result.get("language"),
                               "text_available": bool(result["text"].strip()),
                               "timing": manifest[channel],
                               "offset_seconds": timing["offset_seconds"][channel]}
            for segment in result["segments"]:
                offset = timing["offset_seconds"][channel]
                segments.append({"channel": channel, "start": segment["start"], "end": segment["end"],
                                 "aligned_start": None if offset is None else segment["start"] + offset,
                                 "aligned_end": None if offset is None else segment["end"] + offset,
                                 "text": segment["text"]})
        if not any(track["text_available"] for track in tracks.values()):
            raise HexError("Neither track produced recognized speech; inspect retained results in " + str(output))
        if timing["basis"] != "unavailable":
            segments.sort(key=lambda segment: (segment["aligned_start"], segment["channel"]))
        result = {**info, "state": "ready", "kind": "local_asr", "alignment": timing,
                  "channels": tracks, "segments": segments,
                  "coverage": "automatic transcription of both capture tracks; channel labels are not speaker identities"}
        result["transcript_path"] = str(output / "transcript.md")
        markdown = ["# " + (info.get("title") or identifier), "",
                    "Automatic transcript. Capture channels do not verify speaker identity.",
                    "Alignment: " + timing["basis"] + ". First-arrival alignment is approximate.", ""]
        for segment in segments:
            start = segment["start"] if segment["aligned_start"] is None else segment["aligned_start"]
            markdown.append(f"[{start:.3f}s] [{segment['channel']}] {segment['text'].strip()}")
        atomic_write(Path(result["transcript_path"]), "\n".join(markdown) + "\n")
        receipt = output / "transcript.json"
        result["receipt_path"] = str(receipt)
        write_json(receipt, result)
        return {key: value for key, value in result.items() if key != "segments"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="inspect recordings without starting transcription")
    recovery = commands.add_parser("transcribe", help="return native text or recover both audio tracks")
    recovery.add_argument("recording_id")
    args = parser.parse_args(argv)
    try:
        config = load_config()
        if args.command == "transcribe":
            result = transcribe(config, args.recording_id)
        else:
            root = configured_path(config, "hex_recordings_dir")
            records, errors = [], []
            for directory in sorted(root.iterdir()):
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                try:
                    records.append(inspect(config, directory.name)[0])
                except (OSError, ValueError) as error:
                    errors.append({"id": directory.name, "error": str(error)})
            result = {"recordings": records, "errors": errors,
                      "coverage": "partial" if errors else "complete"}
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": "hex_transcription_error", "detail": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
