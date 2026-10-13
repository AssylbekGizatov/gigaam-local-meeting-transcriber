"""Build-only, bounded public FLEURS fixtures; never included in runtime assets."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import struct
import urllib.error
import urllib.parse
import urllib.request
import wave

import numpy as np

DATASET = "google/fleurs"
REVISION = "70bb2e84b976b7e960aa89f1c648e09c59f894dd"
CONFIGS = ("kk_kz", "ru_ru", "en_us")
CARD = f"https://huggingface.co/datasets/{DATASET}/raw/{REVISION}/README.md"
LICENSE = "https://creativecommons.org/licenses/by/4.0/legalcode.txt"
ATTRIBUTION = "FLEURS: Few-shot Learning Evaluation of Universal Representations of Speech. Alexis Conneau, Min Ma, Simran Khanuja, Yu Zhang, Vera Axelrod, Siddharth Dalmia, Jason Riesa, Clara Rivera, Ankur Bapna (2022). Google FLEURS, CC BY 4.0. https://arxiv.org/abs/2205.12446 ; https://huggingface.co/datasets/google/fleurs"
EN_DATASET = "hf-internal-testing/librispeech_asr_dummy"
EN_REVISION = "5be91486e11a2d616f4ec5db8d3fd248585ac07a"
EN_LOADER = "https://huggingface.co/datasets/hf-internal-testing/librispeech_asr_dummy/raw/7d6b249bbe2b91e6253e2904d23a659994eb5e8e/librispeech_asr_dummy.py"
EN_ATTRIBUTION = "LibriSpeech ASR corpus (Vassil Panayotov, Guoguo Chen, Daniel Povey, Sanjeev Khudanpur, ICASSP 2015), CC BY 4.0, https://www.openslr.org/12/ . One unchanged recording from the Hugging Face Internal Testing LibriSpeech dummy subset; dataset loader links the original OpenSLR corpus."


def fetch(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "GigaAM-Meeting-build-fixtures/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("Response exceeds the small-fixture download bound")
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(directory, name, data):
    with (directory / name).open("xb") as stream:
        stream.write(data)
    return {"file": name, "bytes": len(data), "sha256": digest(data)}


def pcm16(data):
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise RuntimeError("Viewer audio is not a small WAV; no codec or archive fallback is attempted")
    fmt, frames = None, None
    offset = 12
    while offset + 8 <= len(data):
        name, size = struct.unpack_from("<4sI", data, offset)
        start = offset + 8
        if start + size > len(data):
            raise RuntimeError("Invalid WAV chunk size")
        if name == b"fmt ":
            fmt = data[start:start + size]
        if name == b"data":
            frames = data[start:start + size]
        offset = start + size + size % 2
    if fmt is None or len(fmt) < 16 or frames is None:
        raise RuntimeError("WAV format or samples missing")
    code, channels, rate = struct.unpack_from("<HHI", fmt)
    bits = struct.unpack_from("<H", fmt, 14)[0]
    if code == 0xFFFE and len(fmt) >= 40:
        code = struct.unpack_from("<H", fmt, 24)[0]
    if channels != 1 or rate != 16000 or (code, bits) not in {(1, 16), (3, 32)}:
        raise RuntimeError("Only mono 16k PCM16 or float32 WAV fixtures are accepted")
    values = np.frombuffer(frames, dtype="<i2" if code == 1 else "<f4")
    if not 48000 <= len(values) <= 320000 or not np.isfinite(values).all():
        raise RuntimeError("Fixture must be complete, finite, and between three and twenty seconds")
    if code == 1:
        return data, values.copy(), "none; original PCM16 WAV preserved"
    values = np.rint(np.clip(values, -1, 32767 / 32768) * 32768).astype("<i2")
    return wav_bytes(values), values, "float32 WAV converted to PCM16; no resampling or trimming"


def wav_bytes(values):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(values.astype("<i2", copy=False).tobytes())
    return output.getvalue()


def acquire(config, directory):
    attempts = []
    row_doc = None
    for endpoint in ("rows", "first-rows"):
        params = {"dataset": DATASET, "config": config, "split": "test"}
        if endpoint == "rows":
            params.update(offset=0, length=10)
        url = "https://datasets-server.huggingface.co/" + endpoint + "?" + urllib.parse.urlencode(params)
        try:
            row_doc = json.loads(fetch(url, 1_500_000))
            attempts.append({"url": url, "status": 200})
            break
        except urllib.error.HTTPError as error:
            attempts.append({"url": url, "status": error.code, "provider_error": error.read(2048).decode("utf-8", errors="replace")})
        except Exception as error:
            attempts.append({"url": url, "error_type": type(error).__name__, "message": str(error)[:300]})
    if row_doc is None:
        return {"config": config, "status": "unavailable", "attempts": attempts}
    try:
        selected = next(item for item in row_doc["rows"] if not item.get("truncated_cells") and 48000 <= int(item["row"]["num_samples"]) <= 320000)
        row = selected["row"]
        audio = row["audio"]
        src = audio[0]["src"] if isinstance(audio, list) else audio["src"]
        parsed = urllib.parse.urlparse(src)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host in {"huggingface.co", "hf.co"} or host.endswith((".huggingface.co", ".hf.co"))):
            raise RuntimeError("Audio URL is outside official Hugging Face media hosts")
        original = fetch(src, 2_000_000)
        normalized, values, modifications = pcm16(original)
        item = save(directory, config + ".wav", normalized)
        if normalized != original:
            save(directory, config + ".source.wav", original)
        metadata = {"config": config, "status": "acquired", "split": "test", "row_idx": selected["row_idx"],
                    "sample_id": row["id"], "reference": row["transcription"], "raw_reference": row.get("raw_transcription"),
                    "source_url": src, "source_sha256": digest(original), "source_revision": REVISION,
                    "license": "CC-BY-4.0", "attribution": ATTRIBUTION, "modifications": modifications,
                    "seconds": len(values) / 16000, "audio": item, "attempts": attempts}
        return metadata
    except Exception as error:
        return {"config": config, "status": "unavailable", "attempts": attempts,
                "error_type": type(error).__name__, "message": str(error)[:300]}


def english_alternative(directory):
    # SoundFile is already a build-only upstream validation dependency. This
    # helper is not frozen or called by the application, which omits libsndfile.
    import soundfile as sf
    info = json.loads(fetch("https://huggingface.co/api/datasets/" + EN_DATASET, 1_000_000))
    if info.get("sha") != EN_REVISION:
        raise RuntimeError("English subset revision changed; review the fixture pin")
    loader = fetch(EN_LOADER, 100_000)
    if b'http://www.openslr.org/12' not in loader:
        raise RuntimeError("English subset no longer establishes original LibriSpeech provenance")
    original_page = fetch("https://www.openslr.org/12/", 100_000)
    if b"CC BY 4.0" not in original_page:
        raise RuntimeError("Original LibriSpeech license could not be verified")
    save(directory, "LibriSpeech-subset-original-loader.py.txt", loader)
    save(directory, "LibriSpeech-original-source.html", original_page)
    license_file = save(directory, "CC-BY-4.0-full.txt", fetch(LICENSE, 100_000))
    save(directory, "ATTRIBUTION.txt", (EN_ATTRIBUTION + "\nBuild-test fixture only; excluded from runtime and publication assets.\n").encode("utf-8"))
    params = {"dataset": EN_DATASET, "config": "clean", "split": "validation", "offset": 0, "length": 1}
    row_url = "https://datasets-server.huggingface.co/rows?" + urllib.parse.urlencode(params)
    doc = json.loads(fetch(row_url, 100_000))
    selected = doc["rows"][0]
    if selected.get("truncated_cells"):
        raise RuntimeError("English reference or audio was truncated")
    row = selected["row"]
    src = row["audio"][0]["src"]
    if not src.startswith("https://datasets-server.huggingface.co/") or EN_REVISION not in src:
        raise RuntimeError("English viewer media revision/host does not match the verified subset")
    original = fetch(src, 2_000_000)
    values, rate = sf.read(io.BytesIO(original), dtype="int16", always_2d=True)
    if rate != 16000 or values.shape[1] != 1 or not 48000 <= len(values) <= 320000:
        raise RuntimeError("English example is not a complete short mono16k recording")
    source = save(directory, "en_us.source.flac", original)
    audio = save(directory, "en_us.wav", wav_bytes(values[:, 0]))
    report = {"dataset": EN_DATASET, "revision": EN_REVISION, "config": "clean", "split": "validation",
              "row_idx": selected["row_idx"], "sample_id": row["id"], "reference": row["text"],
              "source_url": src, "row_url": row_url, "source": source, "source_sha256": digest(original),
              "audio": audio, "seconds": len(values) / 16000, "license": "CC-BY-4.0", "license_file": license_file,
              "license_source": "https://www.openslr.org/12/", "provenance_source": EN_LOADER,
              "attribution": EN_ATTRIBUTION, "modifications": "lossless FLAC decoded to PCM16 WAV; no resampling or trimming",
              "publication": "audio stays local; publish only results and source/reproduction links with attribution",
              "limitation": "one read-speech example, not a benchmark or real meeting test"}
    save(directory, "english-acquisition.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    print("en_us acquired", report["seconds"], "seconds", audio["sha256"])
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "language-fixtures")
    parser.add_argument("--english-alternative", action="store_true", help="one licensed LibriSpeech subset example instead of unavailable FLEURS viewer")
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise RuntimeError("Fixture directory is not empty; select a fresh destination rather than overwrite files")
    if args.english_alternative:
        return english_alternative(directory)
    info = json.loads(fetch("https://huggingface.co/api/datasets/" + DATASET, 2_000_000))
    if info.get("sha") != REVISION or "cc-by-4.0" not in info.get("cardData", {}).get("license", []):
        raise RuntimeError("Dataset revision/license changed; review the fixture pin before fetching")
    card = save(directory, "FLEURS-dataset-card.md", fetch(CARD, 3_000_000))
    license_file = save(directory, "CC-BY-4.0-full.txt", fetch(LICENSE, 100_000))
    save(directory, "ATTRIBUTION.txt", (ATTRIBUTION + "\nLocal build-test fixtures only; excluded from the portable application and publication assets.\n").encode("utf-8"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        fixtures = list(pool.map(lambda config: acquire(config, directory), CONFIGS))
    report = {"dataset": DATASET, "revision": REVISION, "source_card": CARD, "card": card, "license": license_file,
              "acquired_at_utc": datetime.now(timezone.utc).isoformat(), "fixtures": fixtures,
              "selection": "first eligible complete 3–20 second test row within the first viewer page; no model-output selection",
              "publication": "audio stays local; publish only results and reproduction/source links with attribution",
              "limitation": "Three read-speech examples do not establish real meeting quality or a multilingual benchmark"}
    if all(item["status"] == "acquired" for item in fixtures):
        parts = []
        for index, item in enumerate(fixtures):
            with wave.open(str(directory / item["audio"]["file"]), "rb") as audio:
                parts.append(np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").copy())
            if index < len(fixtures) - 1:
                parts.append(np.zeros(16000, dtype="<i2"))
        report["synthetic_mixed"] = {"audio": save(directory, "mixed_kk_ru_en.wav", wav_bytes(np.concatenate(parts))),
                                     "reference": " ".join(item["reference"] for item in fixtures),
                                     "modifications": "KK then RU then EN complete public recordings concatenated with one-second silence gaps",
                                     "limitation": "different recorded speakers; synthetic concatenation is not natural code switching"}
    save(directory, "acquisition.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    for item in fixtures:
        print(item["config"], item["status"])
    return 0 if all(item["status"] == "acquired" for item in fixtures) else 1


if __name__ == "__main__":
    raise SystemExit(main())
