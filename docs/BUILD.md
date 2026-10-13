# Сборка Windows x64

Эта инструкция предназначена разработчику; пользователю готового ZIP инструменты не нужны. Используйте Windows x64, официальный Python 3.12 x64 с Tcl/Tk, Git и отдельное короткое окружение сборки. Windows long paths включать не требуется: выбирайте короткий путь к venv, например `C:\build\v`.

```powershell
python -m venv C:\build\v
.\build\build.ps1 -Python C:\build\v\Scripts\python.exe -Output C:\build\release
```

Скрипт получает закреплённые wheels из PyPI и официального CPU-индекса PyTorch, GigaAM по immutable commit, модель по immutable revision и полные лицензии. Загрузка отсутствующих весов происходит только здесь. Если уже существующие веса не соответствуют SHA256, сборка останавливается, не заменяет их.

Каждый запуск использует новый каталог `build/reports/<run-id>`: JUnit текущих тестов, attestation встроенных исходников, frozen self-test и сетевой аудит не смешиваются с предыдущими сборками. `build/prepare_release_evidence.py` проверяет актуальные SHA256 исходников и весов, затем по явному списку создаёт публичные отчёты в `build/release-evidence/<run-id>`. Из JUnit убираются hostname и диагностический вывод; абсолютные пользовательские пути и подписанные транспортные ссылки не публикуются. Для acquisition остаются публичные источники, закреплённые revisions и хеши, для минимального GigaAM-патча — проверенные исходники и diff.

Перед упаковкой весь прежний `bundle/provenance` (и прежний `bundle/verification`, если есть) переносится в отдельный `build/private-evidence/<run-id>` после проверки целевых путей. В ZIP копируется только новый curated `provenance`, без слияния с сырыми pip-отчётами; нормализованный license inventory заменяет корневой `dependency-inventory.json`. Сырые build-отчёты остаются локальными и не входят в source ZIP. Публичный `assets/asr-smoke.wav` входит в поставку для offline self-test; приватные записи и английский build fixture в приложение и source ZIP не включаются.

Версии Python, пакетов, ссылки и SHA256 загрузок фиксируются в `dependency-inventory.json` и `provenance`. Проверки исходников и настоящего CPU-inference прошли на Python 3.12.3; финальная rc2 frozen-сборка тоже прошла self-test (36,461 сек, `frozen=true`) с собственным CPU runtime и точным public control-file. CI настроен на 3.12.10. Воспроизводимость побайтно не заявляется (PyInstaller, ZIP timestamp и wheel сборки могут различаться); идентичность официальных весов и исходного GigaAM проверяется.

Минимальный патч `build/patch_gigaam.py` только откладывает неиспользуемый soundfile import. Его исходный SHA256, diff и тест отказа доступа к soundfile фиксируются. Frozen spec исключает soundfile; аудит должен подтвердить отсутствие soundfile/libsndfile/FFmpeg/ASIO в готовом бандле. Массивы PCM передаются напрямую модели. Windows WASAPI работает через встроенный `_portaudiowpatch.pyd`.

Inference использует только Torch/Torchaudio **CPU** и native Conformer/CTC, без ONNX-сессий. `GigaAM-Meeting.spec` исключает ONNX, ONNXRuntime, `ml_dtypes`, `gigaam.onnx_utils` и `gigaam.vad_utils`; CUDA/ONNXRuntime native DLL не допускаются аудитом. upstream зависимости ONNX/ORT и soundfile могут присутствовать в venv сборщика для разрешения закреплённого dependency graph, но они не нужны runtime-пути этого приложения и должны отсутствовать в итоговом бандле.

`dependency-inventory.json`, acquisition evidence и `licenses` намеренно описывают более широкое **сборочное окружение** вместе с компонентами runtime. В них сохраняются версии и лицензии исключённых ONNX/ORT/soundfile; это не заявление об их фактической поставке или использовании. Фактический состав архива устанавливают `file-manifest.json` и PE-аудит. Перед публикацией проверяйте оба слоя и не публикуйте отчёты с hostname, абсолютными пользовательскими путями, выбранными аудиоустройствами или приватной записью.

`GigaAM-Meeting.spec` создаёт onedir EXE с собственными Python/VC runtime/Tcl/Tk/native DLL. Затем копируются модель, шрифт, публичный WAV, инструкции и лицензии. `build/audit_runtime.ps1` запускает EXE с PATH, содержащим только Windows, пустыми PYTHONHOME/PYTHONPATH и выполняет самопроверку/выборку сетевых endpoints. Python не удаляется с машины; это проверка независимости пути, не сертификат чистой виртуальной машины. Не меняются сеть, Firewall или защита Windows.

Для финального rc2 этот runtime-аудит завершился exit code 0: 51 выборка, 0 наблюдений TCP/UDP, 0 попыток Python network guard. `build/verify_source.py` подтвердил соответствие встроенных bytecode/constants/exception tables/stack sizes/names текущим проверенным исходникам, исключив debug paths/line tables. Отдельный запуск на чистой Windows без установленного Python и физически отключённой сети не выполнен.

`build/package.py` проверяет x64 PE imports/delay-imports, запрещённые зависимости, обязательные файлы и полные лицензии; делает манифест с SHA256 каждого файла и ZIP/SHA256 без перезаписи ранее созданных выпусков. GitHub asset должен быть меньше 2 GiB. Финальный PE import/delay-import аудит каталога rc2 прошёл: 126 PE-файлов, отсутствующих зависимостей 0. Проверка CRC публикуемого ZIP остаётся отдельным шагом упаковки. Динамические LoadLibrary не полностью покрываются статическим PE-аудитом; frozen-тест дополняет его.

Сетевой доступ на build-time отделён от `app.py`: обычный запуск не импортирует build-скрипты. Шрифт DejaVu Sans находится в репозитории, источник — официальная копия в matplotlib v3.10.0: `lib/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf`, SHA256 `3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22`. Его полная лицензия находится в `fonts/LICENSE.txt`.


Полная ранее полученная лицензия Microsoft VC runtime включена в `assets/licenses/Microsoft-VC-Runtime-2015-2022-License.docx` (39 644 байта). Build-time проверяет SHA256 `f1e3d56ceb2ad68aae0711b910375009e651ac5530fa0760f0dea6e81e54fae1` и копирует её в `licenses`; оригинальный URL и source page сохраняются в inventory. Это устраняет нестабильную загрузку документа (HTTP 403 на одном CI runner), не обходя ограничения доступа. Изменённый или отсутствующий notice останавливает сборку. Две дополнительные проверки подтверждают отсутствие сетевого запроса для notice и отказ при повреждении. Само приложение не вызывает этот сборочный код.
