# Происхождение и лицензии

Движок локального CPU-распознавания, проверки закреплённых весов и идеи упаковки адаптированы из `AssylbekGizatov/teams-local-transcriber`, компонента `desktop-microphone` (MIT). Автор исходного приложения — Assylbek Gizatov; исходное уведомление сохранено в LICENSE. Новый проект не зависит от его серверов и не изменяет старый репозиторий.

GigaAM: [salute-developers/GigaAM](https://github.com/salute-developers/GigaAM/tree/7447938d791c4f3e643386ee22c33777004293a5), MIT, закреплённый commit. Модель: [ai-sage/GigaAM-Multilingual](https://huggingface.co/ai-sage/GigaAM-Multilingual/tree/2f8a57144e6ec3adfd32fe0484d9ea9913305bc8), MIT согласно опубликованной model card; полный текст лицензии включён рядом с весами. Патч отложенного импорта неиспользуемого soundfile описан в provenance сборки; веса не изменены.

PyAudioWPatch предоставляет WASAPI loopback через PortAudio. Его условия и условия включённого нативного кода сохраняются отдельно. Экспорт DOCX: python-docx/lxml; PDF: ReportLab и DejaVu Sans. Лицензия приложения не заменяет лицензий этих компонентов. Полные тексты и версии доступны в `licenses/` и `dependency-inventory.json` каждого ZIP.

Публичный русский тестовый WAV получен из официального примера GigaAM. Это тестовый материал, не запись пользователя. Его URL и SHA256 приведены в тестах и отчёте самопроверки.
