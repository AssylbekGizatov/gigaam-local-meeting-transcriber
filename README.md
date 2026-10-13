# GigaAM Local Meeting Transcriber

Portable **Windows x64** application with a Russian interface. Transcribe your microphone and the audio playing through your selected output device while you are already in a Teams, Zoom, or other call. WASAPI loopback works with headphones without a virtual audio cable.

The application does not join meetings or create a participant. It uses no meeting accounts, Teams/Zoom SDKs or APIs, Vexa, Docker, cloud speech recognition, or external LLMs. The model and runtime load from local disk. Missing model files produce an error instead of a download.

**Current release: [v0.1.2](https://github.com/AssylbekGizatov/gigaam-local-meeting-transcriber/releases/tag/v0.1.2).** This version adds an optional conservative filter for speaker audio repeated in the microphone transcript. It requires matching words, timing, and audio correlation; it does not mute the microphone or alter recorded audio. One user test confirmed that duplicates disappeared and the user's own speech remained. See [echo filtering and limitations](docs/ECHO.md) and [test results](docs/TESTING.md). The WASAPI clock fix from [v0.1.1](docs/HOTFIX-v0.1.1.md) is retained.

## Start in three steps

1. Download **`GigaAM-Meeting-windows-x64.zip`** and its SHA256 file from the release. Verify the checksum and **extract the entire ZIP into a new local folder**. Keep `_internal`, `models`, `fonts`, `licenses`, and the other bundled files together.
2. Open `GigaAM-Meeting.exe`. Choose your microphone, computer audio device, and export folder. Make sure your call plays through the selected output device. The level check also requires explicit capture consent.
3. Obtain the necessary participant consent, check the capture permission box, and click **«Начать» (Start)**. Use **«Пауза» (Pause)** as needed. Click **«Остановить и завершить текст» (Stop and finish text)**, wait for processing to finish, and export TXT, MD, DOCX, or PDF.

End users do not need Python, pip, Git, Docker, or a terminal. The GitHub **Source code** archives and `GigaAM-Meeting-source-…zip` are for developers; they are not the runnable application. Do not launch the EXE from inside the ZIP or copy only the EXE to another folder. Respect Windows security controls and organizational policy; contact your administrator if execution is blocked.

## Audio sources and consent

**Computer audio captures every application playing through the selected output device**, including notifications. Capturing a single application is not supported in this release. The application does not silently switch to another device. If a headset changes or disconnects, stop, refresh the device list, choose the new devices, and explicitly start a new session. Capture never starts automatically or resumes after restarting the application.

The two sources share a session clock that accounts for pauses. **“Microphone” and “Computer audio” identify audio sources, not individual speakers.** The application does not infer names or the number of participants.

With both sources enabled, the duplicate filter is on by default and can be disabled before a session. Confirmed repeated microphone words are removed from display and export; the computer audio transcript and separate source audio remain intact. This is transcript filtering, not full acoustic echo cancellation. Short or ambiguous repetitions can remain. Overlapping voices within one source and quiet local speech under loud speaker audio can reduce accuracy. Headphones are recommended.

Windows microphone permission does not replace participant consent or organizational recording rules. Capture has a persistent visible indicator and requires an explicit user action for each session.

## Speech recognition

The application retains **GigaAM Multilingual 220M CTC** with local CPU float32 inference:

| Component | Pinned revision |
| --- | --- |
| GigaAM code | `7447938d791c4f3e643386ee22c33777004293a5` |
| Model | `2f8a57144e6ec3adfd32fe0484d9ea9913305bc8` |

Configuration and weight SHA256 values are checked before loading. The model supports Kazakh, Russian, English, and mixed speech; broad real-device language quality has not been certified. Check important transcripts against the conversation.

Fragments usually become available after a pause or around eight seconds of continuous speech, plus inference time. Latency depends on the CPU and active sources. The application uses boundary context, timestamp confirmation, and a bounded queue holding up to 48 seconds of audio. If processing falls behind, the interface reports it. On overflow, new blocks are skipped with a visible warning. Stopping capture drains pending fragments before export. Noise, short words, silence, language changes, and fragment boundaries can still cause errors.

## Privacy and local storage

Audio saving is **off by default**. Without it, PCM stays in bounded memory buffers; no temporary audio files are created. Transcripts remain in memory until explicitly exported. Enabling audio saving creates a new `audio-…` folder with separate source WAV files and a `timeline.json` recording timing, gaps, and parameters. WAV files rotate approximately every ten minutes. Exports do not overwrite existing files.

There is no telemetry, crash-report upload, or conversation content in diagnostic logs. Python network access is blocked inside the application process. This guard and sampled TCP/UDP checks are limited evidence, not a system-wide network guarantee. Windows paging or crash dumps may retain memory, and your chosen folder may be backed up or synchronized by OneDrive. Choose storage and retention policies accordingly. See [PRIVACY.md](docs/PRIVACY.md) for the method and limits.

## Documentation and maintenance

| Document | Purpose |
| --- | --- |
| **[DevOps runbook / Памятка DevOps](docs/DEVOPS_RUNBOOK.md)** | Detailed Russian guide: sequential commands for download, checksum verification, installation, diagnostics, source builds, GitHub releases, updates, and rollback |
| [BUILD.md](docs/BUILD.md) | Build pipeline, pinned dependencies, licenses, and evidence handling |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Capture, buffering, inference, session lifecycle, and export design |
| [TESTING.md](docs/TESTING.md) | Automated evidence, user acceptance, and remaining manual checks |
| [ECHO.md](docs/ECHO.md) | Duplicate filtering behavior and limitations |
| [PRIVACY.md](docs/PRIVACY.md) | Offline runtime and local data handling |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

Dependency and model acquisition require internet access **during builds only**. GitHub Actions builds a portable bundle; documentation-only commits on `main` do not replace existing release assets. New code versions require a new release tag and verification.

## Licenses

Application code is MIT licensed. The complete runtime ZIP includes component notices for the model, Python, GigaAM, PyAudioWPatch/PortAudio, PyTorch, NumPy, SentencePiece, Tcl/Tk, export libraries, fonts, and runtime dependencies. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md) for licensing and reused-code provenance. The original `teams-local-transcriber` remains a separate project.