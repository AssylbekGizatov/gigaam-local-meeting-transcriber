# Памятка DevOps: развёртывание и сопровождение GigaAM Local Meeting Transcriber

Актуально для **v0.1.2**, 14 октября 2026 года. Это последовательная инструкция для человека, который передаёт приложение пользователям, собирает новую версию или выпускает обновления. Главный [README](../README.md) — на английском; эта памятка — на русском.

## 0. Сначала выберите сценарий

| Что нужно | Какие разделы выполнить |
| --- | --- |
| Установить готовую программу / передать коллеге | 1–7 |
| Обновить установленную программу / откатить | 8 |
| Собрать из исходников на Windows | 9–11 |
| Выпустить новую версию в GitHub | 12–13 |
| Разобрать ошибку / передать сопровождение | 14–16 |

**Это интерактивное приложение, а не сервер.** Развёртывание означает доставить полный ZIP на Windows x64 и открыть EXE в пользовательском сеансе. Не нужны Docker, Vexa, сервер, входящие порты, аккаунт бота, токены Teams/Zoom или виртуальный аудиокабель. Windows Service, headless-сервер, автозапись и запуск захвата через планировщик не поддерживаются. Перед каждым сеансом пользователь выбирает устройства, явно разрешает захват и сам нажимает «Начать».

Команды ниже выполняйте по порядку **в одной обычной PowerShell-сессии**, чтобы сохранялись переменные. Можно использовать PowerShell 7; блоки установки также написаны для Windows PowerShell 5.1. Сборочные скрипты рассчитаны на PowerShell 7, как в CI. Останавливайтесь при ошибке; не копируйте сразу весь документ в терминал. Команды установки/сборки здесь не требуют изменения защиты Windows, ExecutionPolicy, Firewall, реестра или установки виртуальных драйверов. Если политика организации запрещает действие, согласуйте его с администратором.

## 1. Что скачать и что передавать

Официальная [страница v0.1.2](https://github.com/AssylbekGizatov/gigaam-local-meeting-transcriber/releases/tag/v0.1.2).

| Файл | Назначение |
| --- | --- |
| `GigaAM-Meeting-windows-x64.zip` | **Готовое приложение**: EXE, runtime, зависимости, модель, шрифты и лицензии |
| `GigaAM-Meeting-windows-x64.zip.sha256` | Контрольная сумма именно этого ZIP |
| `GigaAM-Meeting-windows-x64-file-manifest.json` | Размеры и SHA256 файлов внутри приложения |
| `GigaAM-Meeting-windows-x64-dll-audit.json` | Статический PE import/delay-import аудит |
| GitHub `Source code (zip/tar.gz)` или `GigaAM-Meeting-source-…zip` | Исходники для разработчика; **не готовое приложение** |

Передавайте пользователю runtime ZIP и соответствующий SHA256. Не отправляйте только EXE. Не переименовывайте source ZIP так, будто он содержит программу.

Официальный runtime ZIP **v0.1.2 из GitHub Release**: 990 459 876 байт, SHA256:

```text
fe024933fc2de6f1aad75a6177c1ec5080db66c3bf91d494623dcf23ba38435c
```

Отдельная локальная сборка той же версии имеет другой SHA256. Не сверяйте локальный ZIP с хешем CI: архивы не заявлены побайтно воспроизводимыми. Всегда проверяйте пару «конкретный ZIP + его checksum». SHA256 подтверждает целостность относительно опубликованного значения; sidecar на том же GitHub не является независимой цифровой подписью. Подписанный Authenticode EXE в этой версии не заявлен.

## 2. Скачать готовый ZIP без перезаписи

Пользователю Python и Git не нужны. Интернет требуется для этого скачивания; обычная работа распакованного приложения офлайн.

### 2.1. Задать версию и создать отдельный каталог

```powershell
$ErrorActionPreference = 'Stop'
$taskRepo = 'AssylbekGizatov/gigaam-local-meeting-transcriber'
$taskVersion = 'v0.1.2'
$taskBase = Join-Path $env:LOCALAPPDATA 'GigaAMMeeting'
$taskId = [Guid]::NewGuid().ToString('N')
$taskDownload = Join-Path $taskBase ("downloads/" + $taskVersion + "-" + $taskId)
if (Test-Path -LiteralPath $taskDownload) { throw 'Каталог уже существует' }
New-Item -ItemType Directory -Path $taskDownload | Out-Null
```

Используется локальная пользовательская папка, отдельная от транскрипций. Не переносите чужие существующие файлы и не устанавливайте поверх предыдущей версии.

### 2.2. Получить список файлов конкретного Release

```powershell
$taskHeaders = @{ 'User-Agent' = 'GigaAMDeployment'; 'Accept' = 'application/vnd.github+json' }
$taskRelease = Invoke-RestMethod -Uri ("https://api.github.com/repos/" + $taskRepo + "/releases/tags/" + $taskVersion) -Headers $taskHeaders
$taskZipAssets = @($taskRelease.assets | Where-Object { $_.name -eq 'GigaAM-Meeting-windows-x64.zip' })
$taskShaAssets = @($taskRelease.assets | Where-Object { $_.name -eq 'GigaAM-Meeting-windows-x64.zip.sha256' })
if ($taskZipAssets.Count -ne 1 -or $taskShaAssets.Count -ne 1) { throw 'Нет однозначной пары ZIP/SHA256' }
$taskZipAsset = $taskZipAssets[0]
$taskShaAsset = $taskShaAssets[0]
$taskZip = Join-Path $taskDownload $taskZipAsset.name
$taskSha = Join-Path $taskDownload $taskShaAsset.name
if ((Test-Path -LiteralPath $taskZip) -or (Test-Path -LiteralPath $taskSha)) { throw 'Файлы уже существуют' }
```

Для публичного скачивания GitHub-токен не нужен. Ошибку 404/403, ограничение сети или API rate limit не обходите; можно открыть страницу Release и скачать эти же два файла браузером в новый каталог.

### 2.3. Скачать оба файла

```powershell
Invoke-WebRequest -Uri $taskZipAsset.browser_download_url -OutFile $taskZip -UseBasicParsing
Invoke-WebRequest -Uri $taskShaAsset.browser_download_url -OutFile $taskSha -UseBasicParsing
Get-Item -LiteralPath $taskZip | Select-Object Name, Length
```

Прерванное скачивание не распаковывайте. При повторной попытке создайте новый каталог по шагу 2.1, чтобы не затереть файлы.

## 3. Проверить SHA256 и полностью распаковать

### 3.1. Сверить checksum, размер и доступный GitHub digest

```powershell
$taskChecksumLine = (Get-Content -LiteralPath $taskSha -Raw).Trim()
$taskExpected = ($taskChecksumLine -split '[ ]+')[0].ToLowerInvariant()
if ($taskExpected -notmatch '^[0-9a-f]{64}$') { throw 'Неверный формат SHA256' }
$taskActual = (Get-FileHash -LiteralPath $taskZip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($taskActual -ne $taskExpected) { throw 'SHA256 не совпал: НЕ запускайте архив' }
if ((Get-Item -LiteralPath $taskZip).Length -ne $taskZipAsset.size) { throw 'Размер ZIP не совпал' }
if ($taskZipAsset.digest -and $taskZipAsset.digest -ne ("sha256:" + $taskActual)) { throw 'GitHub digest не совпал' }
if ($taskVersion -eq 'v0.1.2' -and $taskActual -ne 'fe024933fc2de6f1aad75a6177c1ec5080db66c3bf91d494623dcf23ba38435c') {
    throw 'Официальный v0.1.2 отличается от значения в памятке: выясните причину'
}
Write-Output ("SHA256 OK: " + $taskActual)
```

Последняя проверка относится к **официальному GitHub-архиву**, а не вашей независимой локальной сборке.

### 3.2. Распаковать в новый каталог

```powershell
$taskInstall = Join-Path $taskBase ("apps/" + $taskVersion + "-" + $taskId)
if (Test-Path -LiteralPath $taskInstall) { throw 'Каталог установки уже существует' }
Expand-Archive -LiteralPath $taskZip -DestinationPath $taskInstall
$taskApp = Join-Path $taskInstall 'GigaAM-Meeting'
$taskExe = Join-Path $taskApp 'GigaAM-Meeting.exe'
$taskRequired = @('GigaAM-Meeting.exe', '_internal', 'models/gigaam/config.json',
    'models/gigaam/pytorch_model.bin', 'fonts', 'licenses', 'provenance',
    'dependency-inventory.json', 'file-manifest.json', 'runtime-dll-audit.json')
foreach ($taskRelative in $taskRequired) {
    if (-not (Test-Path -LiteralPath (Join-Path $taskApp $taskRelative))) {
        throw ("Неполная распаковка: " + $taskRelative)
    }
}
Write-Output ("Приложение: " + $taskExe)
```

Архив содержит корневую папку `GigaAM-Meeting`. Не используйте `-Force` для распаковки поверх существующей установки. Нужен запас диска для ZIP, распакованной программы и отдельно ваших данных; точный объём зависит от сборки. Не открывайте EXE из виртуальной папки ZIP в Проводнике.

### 3.3. Проверить все файлы по встроенному манифесту

Это занимает время, потому что повторно читает модель и runtime. Проверка путей предотвращает обращение за пределы новой установки.

```powershell
$taskManifest = Get-Content -LiteralPath (Join-Path $taskApp 'file-manifest.json') -Raw | ConvertFrom-Json
$taskRoot = [System.IO.Path]::GetFullPath($taskApp).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
$taskPrefix = $taskRoot + [System.IO.Path]::DirectorySeparatorChar
foreach ($taskEntry in $taskManifest) {
    if ([System.IO.Path]::IsPathRooted($taskEntry.file)) { throw 'Абсолютный путь в манифесте' }
    $taskPath = [System.IO.Path]::GetFullPath((Join-Path $taskRoot $taskEntry.file))
    if (-not $taskPath.StartsWith($taskPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Путь манифеста выходит за каталог приложения'
    }
    $taskItem = Get-Item -LiteralPath $taskPath
    if ($taskItem.PSIsContainer -or $taskItem.Length -ne $taskEntry.size) { throw ("Размер не совпал: " + $taskEntry.file) }
    $taskFileHash = (Get-FileHash -LiteralPath $taskPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($taskFileHash -ne $taskEntry.sha256) { throw ("Хеш не совпал: " + $taskEntry.file) }
}
Write-Output ("Проверено файлов: " + @($taskManifest).Count)
```

Сам манифест не перечисляет себя. Его целостность вместе с остальными файлами проверена внешним SHA256 ZIP. Не заменяйте файлы вручную, чтобы «добиться» успешной проверки; заново получите проверенный полный архив.

## 4. Открыть приложение и настроить захват вручную

### 4.1. Создать папку транскрипций отдельно от программы

```powershell
$taskTranscripts = Join-Path $taskBase 'transcripts'
if (-not (Test-Path -LiteralPath $taskTranscripts)) {
    New-Item -ItemType Directory -Path $taskTranscripts | Out-Null
}
if (-not (Get-Item -LiteralPath $taskTranscripts).PSIsContainer) { throw 'Путь занят файлом' }
Write-Output ("Выберите эту папку в интерфейсе: " + $taskTranscripts)
```

Команда только создаёт папку. В приложении нажмите «Выбрать…» и укажите её сами. CLI-флагов для настройки экспорта, записи или автоматического выбора устройств нет.

### 4.2. Запустить видимое окно

```powershell
Start-Process -FilePath $taskExe -WorkingDirectory $taskApp -WindowStyle Normal
```

Или откройте EXE двойным щелчком. Открытие окна не начинает захват.

### 4.3. Действия пользователя в окне — строго по порядку

1. Убедитесь, что в заголовке версия **0.1.2**.
2. Выберите физический микрофон и loopback соответствующего устройства вывода.
3. В Teams/Zoom выберите **то же устройство воспроизведения**, что в поле «Звук компьютера». С наушниками захватывается их цифровой звук; виртуальный кабель не нужен.
4. Прочитайте предупреждение: слышны **все приложения выбранного устройства**, в том числе уведомления. Захвата отдельного приложения нет.
5. Получите нужные согласия и отметьте разрешение **перед** кнопкой «Проверить уровни · 3 сек». Это явная краткая проверка выбранных источников. После неё разрешение сбрасывается; для основной записи его нужно отметить снова.
6. Выберите папку экспорта. Опцию дополнительного сохранения WAV оставьте выключенной, если аудиозапись не нужна.
7. Для двух источников оставьте фильтр дублей включённым. Переключение действует на следующий сеанс; это фильтр текста, не акустическое подавление.
8. Получите нужные согласия участников и учтите правила организации. Разрешение Windows на микрофон их не заменяет.
9. Отметьте разрешение захвата и **сами нажмите «Начать»**. Проверьте постоянный индикатор и уровни обоих источников.
10. Текст появляется фрагментами с задержкой. Следите за очередью и предупреждениями; не считайте ранние фрагменты безошибочными.
11. Для перерыва используйте «Пауза». Для завершения нажмите «Остановить и завершить текст» и дождитесь пустой очереди.
12. Сохраните выбранный формат и проверьте файл. Для следующего сеанса снова требуется явное разрешение.

Названия «Микрофон» и «Звук компьютера» — источники, а не имена людей. Обновление текста после подтверждения дублей может убрать ранее показанные повторные слова микрофона. Экспорт использует итоговый текст. Не запускайте несколько экземпляров для одной записи.

## 5. Диагностика готового EXE без Python и без живой записи

Перед диагностикой завершите активный захват в интерфейсе и сохраните нужный текст. Каждый отчёт создавайте с новым именем. Не отправляйте каталог диагностики целиком в публичный GitHub.

### 5.1. Создать новый каталог отчётов

```powershell
$taskReports = Join-Path $taskBase ("reports/" + [Guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $taskReports) { throw 'Каталог отчётов уже существует' }
New-Item -ItemType Directory -Path $taskReports | Out-Null
```

### 5.2. Перечислить устройства, не открывая запись

```powershell
$taskDeviceReport = Join-Path $taskReports 'devices.json'
$taskDeviceProc = Start-Process -FilePath $taskExe -WorkingDirectory $taskApp -WindowStyle Hidden -Wait -PassThru -ArgumentList @('--list-devices', ('"' + $taskDeviceReport + '"'))
if ($taskDeviceProc.ExitCode -ne 0) { throw 'Не удалось получить список устройств' }
Get-Content -LiteralPath $taskDeviceReport -Raw
```

`--list-devices` перечисляет метаданные PortAudio, не запускает аудиопотоки. Список может раскрывать названия устройств; оставляйте его локальным. Родительская папка отчёта должна существовать.

### 5.3. Выполнить offline self-test на публичном контрольном PCM

```powershell
$taskSelfReport = Join-Path $taskReports 'selftest.json'
$taskSelfProc = Start-Process -FilePath $taskExe -WorkingDirectory $taskApp -WindowStyle Hidden -Wait -PassThru -ArgumentList @('--self-test', ('"' + $taskSelfReport + '"'))
if ($taskSelfProc.ExitCode -ne 0) { throw 'Self-test завершился с ошибкой; проверьте отчёт' }
$taskSelf = Get-Content -LiteralPath $taskSelfReport -Raw | ConvertFrom-Json
if ($taskSelf.status -ne 'passed') { throw 'Self-test не прошёл' }
if ($taskSelf.blocked_python_network_attempts -ne 0) { throw 'Зафиксирована попытка Python network access' }
$taskSelf | Select-Object status, elapsed_seconds, physical_capture_tested, blocked_python_network_attempts
```

Этот режим не захватывает микрофон или системный звук. Он проверяет закреплённые локальные веса, настоящий GigaAM inference на публичном WAV, синтетическое эхо, ограниченную очередь и четыре формата экспорта. Рядом с JSON появляются **явные тестовые** TXT/MD/DOCX/PDF в `selftest-artifacts`. Это не скрытая запись разговора.

В v0.1.2 контроль эха распознаёт 26 слов компьютерного источника и 26 соответствующих слов синтетического микрофонного эха, удаляет подтверждённые повторы и сохраняет системный текст. Проверяйте раздел `fixture` в отчёте. Self-test не подтверждает качество вашего микрофона, естественный казахский или реальную длительную встречу. `physical_capture_tested=false` здесь правильно даже после ручного теста: это другой метод проверки.

Не применяйте флаги `--start`, `--meeting-url`, `--token`, `--device`: приложение их не поддерживает. Два описанных диагностических флага не запускают живую запись.

## 6. Проверка сети: метод и честные границы

Сам runtime загружает модель с диска, не скачивает её и не отправляет аудио, текст, телеметрию или crash reports. Python socket guard запрещает сетевые операции в процессе. Это не системный Firewall.

Для инженера с исходниками доступен аудит `build/audit_runtime.ps1`. Он временно ограничивает **окружение дочернего процесса** Windows PATH и очищает PYTHONHOME/PYTHONPATH, затем возвращает значения. Windows и её настройки не меняются. Выполните из корня исходников, после раздела 9:

```powershell
Get-Command Get-NetTCPConnection, Get-NetUDPEndpoint -ErrorAction Stop | Out-Null
Get-NetTCPConnection -ErrorAction Stop | Out-Null
Get-NetUDPEndpoint -ErrorAction Stop | Out-Null
$taskAuditDir = Join-Path $taskBuildRoot ("network-audit-" + [Guid]::NewGuid().ToString('N'))
./build/audit_runtime.ps1 -Exe $taskExe -ReportDirectory $taskAuditDir
$taskAudit = Get-Content -LiteralPath (Join-Path $taskAuditDir 'runtime-network-audit.json') -Raw | ConvertFrom-Json
$taskAudit
```

Предварительные команды должны иметь доступ к таблицам endpoints. Если доступа нет, результат «0 наблюдений» не доказывает отсутствие сети. Сам скрипт подавляет отдельные ошибки выборок; проверка доступа перед запуском не исключает все последующие сбои.

Метод: выборка TCP/UDP endpoints PID во время короткого self-test, примерно каждые 250 мс плюс время запросов. Может пропустить короткие соединения, активность нативных компонентов через другие процессы и события между выборками. В локальном v0.1.2 было 45 выборок, 0 TCP/UDP и 0 попыток Python guard; CI имеет собственный отчёт внутри ZIP.

Проверка PATH независимости не означает, что Python физически удалён с машины. **Чистая Windows без установленного Python и физически отключённой сетью ещё не проверена.** Для такого приёмочного теста используйте отдельную разрешённую тестовую машину/VM: скопируйте проверенный ZIP, отключите сетевой адаптер тестовой VM штатно, откройте EXE, выполните self-test и ручной захват с согласиями. Зафиксируйте конфигурацию и результат. Не изменяйте Firewall или защиту рабочего компьютера ради теста и не публикуйте заключение раньше фактического прохождения.

## 7. Данные, лицензии и передача другому пользователю

Без опции аудиозаписи звук находится в ограниченных буферах памяти; временные WAV не создаются. Фильтр эха использует дополнительно ограниченную историю PCM: до 9 216 000 байт для двух источников. Очереди ограничены, но накопленный текст длительной сессии растёт.

При включении WAV создаются раздельные каналы и `timeline.json`; ротация приблизительно каждые десять минут. Фильтр повторов **не очищает WAV**, не смешивает источники и не меняет звук для модели. Готовый экспорт создаётся только по действию пользователя, без перезаписи существующего файла.

Подкачка ОС, дампы, резервные копии и синхронизация папки экспорта контролируются окружением пользователя. Приложение не гарантирует физическое стирание RAM/диска. Согласуйте папку и сроки хранения; не удаляйте содержимое чужих папок автоматически.

В ZIP обязательно сохраняйте `licenses`, `NOTICE.md`, license inventory и provenance. Модель остаётся той же **GigaAM Multilingual 220M CTC**, без внешней LLM или cloud STT. Получение зависимостей на этапе сборки не является поведением runtime.

## 8. Обновление и откат без потери данных

### 8.1. Обновление

1. В старом окне нажмите «Остановить и завершить текст», дождитесь обработки, сохраните нужные форматы. Не закрывайте окно с несохранённым текстом.
2. Скачайте **новую версию** в новый каталог по разделу 2. Замените только значение `$taskVersion`; старые папки не трогайте.
3. Проверьте новый SHA256 и новую release evidence. Приведённый в разделе 1 фиксированный SHA относится исключительно к v0.1.2.
4. Полностью распакуйте в новый каталог по разделу 3, выполните self-test.
5. Закройте сохранённое старое окно обычным способом. Откройте новое; выберите устройства, папку данных и разрешение заново.
6. Выполните короткий ручной тест обоих источников. Храните предыдущую установку до успешной приёмки.

Не перезаписывайте работающий EXE или `_internal`. Не переносите модель от другой версии и не смешивайте каталоги. Не восстанавливайте запись после обновления. Расположение транскрипций отделено от версии приложения.

### 8.2. Откат

Остановите новую запись, дозавершите и экспортируйте текст. Закройте окно. Найдите сохранённый EXE предыдущей полной установки и откройте его; **не** заменяйте отдельные DLL в новой версии. Для поиска, без запуска:

```powershell
Get-ChildItem -LiteralPath (Join-Path $taskBase 'apps') -Directory | Select-Object Name, FullName
```

После ручного выбора старого каталога:

```powershell
$taskRollbackRoot = Read-Host 'Полный путь к сохранённой папке GigaAM-Meeting предыдущей версии'
$taskRollbackExe = Join-Path $taskRollbackRoot 'GigaAM-Meeting.exe'
if (-not (Test-Path -LiteralPath $taskRollbackExe -PathType Leaf)) { throw 'EXE не найден' }
Start-Process -FilePath $taskRollbackExe -WorkingDirectory $taskRollbackRoot -WindowStyle Normal
```

Пользователь выбирает устройства и запускает захват заново. Старые экспорты остаются в папке данных. Эта памятка намеренно не содержит рекурсивного удаления установок или данных.

## 9. Подготовить Windows для сборки из исходников

Этот раздел нужен только разработчику. Пользователю готового ZIP ничего из него не требуется. Рекомендуется GitHub Actions из раздела 12 или отдельная Windows x64 сборочная машина.

Нужны официальный **Python 3.12.10 x64 с Tcl/Tk**, Git и PowerShell 7. Источники: [Python 3.12.10](https://www.python.org/downloads/release/python-31210/), [Git for Windows](https://git-scm.com/downloads/win), [PowerShell](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows). Установите разрешённым организацией способом. Нужен интернет для зависимостей и модели. Не меняйте Windows long paths: используйте короткий разрешённый каталог.

### 9.1. Проверить инструменты

```powershell
$ErrorActionPreference = 'Stop'
$taskRepo = 'AssylbekGizatov/gigaam-local-meeting-transcriber'
git --version
if ($LASTEXITCODE -ne 0) { throw 'Git недоступен' }
py -3.12 -c "import sys, struct, tkinter; print(sys.version); print('bits', struct.calcsize('P') * 8); assert sys.version_info[:3] == (3, 12, 10); assert struct.calcsize('P') == 8"
if ($LASTEXITCODE -ne 0) { throw 'Нужен Python 3.12.10 x64 с Tcl/Tk' }
$PSVersionTable.PSVersion
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Для этой сборочной инструкции откройте PowerShell 7' }
```

При отсутствии `py` используйте полный путь к разрешённой установке Python, сохранив проверки. Не перепутайте с Python из Microsoft Store alias. Неподходящий PATH не исправляйте системными изменениями без необходимости.

### 9.2. Выбрать новый короткий путь и получить опубликованные исходники

Пример `C:/gigaam-build-01` подходит только если вы имеете право создавать там папку. Иначе задайте другой короткий разрешённый путь; если это невозможно, используйте CI.

```powershell
$taskBuildRoot = 'C:/gigaam-build-01'
if (Test-Path -LiteralPath $taskBuildRoot) { throw 'Выберите новый каталог сборки' }
New-Item -ItemType Directory -Path $taskBuildRoot | Out-Null
$taskSource = Join-Path $taskBuildRoot 'src'
git clone --branch v0.1.2 --depth 1 ("https://github.com/" + $taskRepo + ".git") $taskSource
if ($LASTEXITCODE -ne 0) { throw 'Не удалось получить исходники' }
Set-Location -LiteralPath $taskSource
$taskCommit = (git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $taskCommit -ne '810bae0cd5ddf498933407860d22bb6a4f9d4b13') { throw 'Неожиданный commit для v0.1.2' }
git status --short
```

Тег фиксирует исходники опубликованной сборки. Последующие документационные коммиты находятся в `main`; не передвигайте опубликованный тег. Для разработки **следующей версии** нужен отдельный обычный checkout `main`, а не редактирование detached tag checkout.

### 9.3. Создать отдельное venv без activation scripts

```powershell
$taskVenv = Join-Path $taskBuildRoot 'v'
py -3.12 -m venv $taskVenv
if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать venv' }
$taskPython = Join-Path $taskVenv 'Scripts/python.exe'
& $taskPython -c "import sys, tkinter; print(sys.version); print('Tcl', tkinter.TclVersion)"
if ($LASTEXITCODE -ne 0) { throw 'Venv или Tcl/Tk не работают' }
```

Команды используют явный путь к Python: `Activate.ps1` и изменение ExecutionPolicy не нужны. Не используйте частично установленное окружение от неудачной старой сборки.

## 10. Собрать полную переносимую программу

### 10.1. Запустить единую последовательность сборки

Из `$taskSource`, после раздела 9:

```powershell
$taskReleaseOut = Join-Path $taskBuildRoot ("release-" + [Guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $taskReleaseOut) { throw 'Каталог выпуска уже существует' }
./build/build.ps1 -Python $taskPython -Output $taskReleaseOut
```

Это не запуск пользовательской записи. Скрипт выполняет проверки на публичном/синтетическом PCM. При ошибке не продолжайте публикацию и не вручную копируйте половину `dist`.

### 10.2. Что именно делает build.ps1 — по порядку

1. Создаёт отдельный `build/reports/<run-id>`.
2. Устанавливает закреплённые runtime/build зависимости из requirements, записывает acquisition reports.
3. Устанавливает `torch==2.10.0` и `torchaudio==2.10.0` из официального CPU-индекса.
4. Получает GigaAM по commit `7447938d791c4f3e643386ee22c33777004293a5`, выполняет `pip check`.
5. Получает модель по revision `2f8a57144e6ec3adfd32fe0484d9ea9913305bc8`, проверяет SHA256 конфигурации/весов.
6. Применяет минимальный проверяемый lazy-import патч неиспользуемого soundfile и фиксирует исходный хеш/diff.
7. Выполняет pytest с JUnit и Ruff; ошибка любого gate останавливает сборку.
8. Собирает onedir EXE через PyInstaller. Исключает ненужные CUDA, ONNX/ORT, soundfile/libsndfile/FFmpeg runtime-компоненты.
9. Сравнивает встроенные Python modules с текущими исходниками: bytecode/constants, exception tables, names и stack sizes.
10. Копирует модель, шрифт, публичный контрольный WAV, документацию, NOTICE и полные лицензии.
11. Формирует dependency inventory и notices.
12. Запускает готовый EXE self-test и ограниченный аудит сетевых endpoints.
13. Создаёт curated release evidence по явному allowlist, проверяет свежесть JUnit и исходников.
14. Переносит старое bundle evidence в приватный build-каталог после проверки путей; помещает в ZIP только новое очищенное provenance.
15. Проверяет Windows x64 PE imports/delay-imports и обязательные файлы.
16. Создаёт file manifest, runtime ZIP и SHA256 без перезаписи существующих выходных архивов.

Не меняйте модель, dtype или провайдер inference для ускорения без согласования. Не запускайте patch-скрипт многократно вручную на произвольном site-packages: используйте свежий venv и штатный pipeline.

Закреплённые SHA256 модели:

| Файл | SHA256 |
| --- | --- |
| `config.json` | `c830232c7d51688a630a221517b52585ab5ee57e1d3c21bcbae01759351d2653` |
| `pytorch_model.bin` | `e1db43873ec5e296f229572e06e2470fc157ac9f8d4aacabda295630b9b91728` |

### 10.3. Лицензия Microsoft VC runtime

Полный оригинальный notice находится в `assets/licenses/Microsoft-VC-Runtime-2015-2022-License.docx`: **39 644 байта**, SHA256 `f1e3d56ceb2ad68aae0711b910375009e651ac5530fa0760f0dea6e81e54fae1`. Скрипт проверяет и копирует его без сетевого запроса за этим документом. Оригинальная публичная ссылка сохранена в inventory.

Это устраняет HTTP 403 внешнего vendor endpoint, который однажды остановил CI. Нельзя заменить notice короткой ссылкой, удалить его ради успешной сборки или обходить ограничение сайта. При несовпадении восстановите файл из проверенных исходников, а не меняйте ожидаемый хеш на произвольный.

## 11. Проверить локальные артефакты перед передачей

### 11.1. Контрольная сумма и CRC собственного ZIP

Используйте хеш **собственной сборки**, не константу официального v0.1.2 из раздела 1.

```powershell
$taskBuiltZip = Join-Path $taskReleaseOut 'GigaAM-Meeting-windows-x64.zip'
$taskBuiltSha = $taskBuiltZip + '.sha256'
$taskBuiltExpected = ((Get-Content -LiteralPath $taskBuiltSha -Raw).Trim() -split '[ ]+')[0]
$taskBuiltActual = (Get-FileHash -LiteralPath $taskBuiltZip -Algorithm SHA256).Hash
if ($taskBuiltActual -ne $taskBuiltExpected) { throw 'Хеш собственного ZIP не совпал' }
& $taskPython -c "import sys, zipfile; z=zipfile.ZipFile(sys.argv[1]); bad=z.testzip(); z.close(); print('CRC:', bad or 'OK'); assert bad is None" $taskBuiltZip
if ($LASTEXITCODE -ne 0) { throw 'CRC ZIP не прошёл' }
Get-ChildItem -LiteralPath $taskReleaseOut | Select-Object Name, Length
```

Затем распакуйте свой ZIP в новый каталог, проверьте file manifest по разделу 3.3 и выполните self-test именно **распакованного** EXE. Это выявляет неполную упаковку. Библиотеки build venv не должны маскировать отсутствие runtime DLL.

### 11.2. Провести ручную приёмку, которую self-test не заменяет

| Проверка | Действие / что записать в отчёте |
| --- | --- |
| Только микрофон | Своя короткая речь, уровни, текст, таймкоды |
| Только системный звук | Разрешённый тестовый голос через выбранный выход, текст |
| Оба источника | Удалённая речь и свой ответ; текст обоих источников |
| Динамики и эхо | Повторение системной речи в микрофоне; повторы уменьшаются, свой ответ остаётся |
| Наушники | Корректный loopback выбранных наушников без виртуального кабеля |
| Одновременная речь | Свой ответ во время компьютерного голоса; также тихий ответ на фоне громких динамиков |
| Пауза / Stop / новый сеанс | Нет скрытой записи, очередь дозавершается, новый сеанс требует разрешения |
| Отключение / смена устройств | Видимая ошибка/остановка, никакой незаметной подмены источника |
| Длинная сессия | Фактическая длительность, CPU, память, задержка, gaps/переполнения |
| Языки | Отдельно RU, EN, KK и естественная смешанная речь; сравнить с известным текстом |
| Экспорт | TXT/MD/DOCX/PDF: Unicode, таймкоды, итоговый текст, файл не перезаписан |
| Офлайн переносимость | Отдельная Windows без Python, сеть отключена, полный ZIP, self-test и ручной захват |

Укажите фактическую длительность и конфигурацию, а не только «работает». Нет заданного доказанного SLA или гарантированной максимальной длительности. Приёмка на одной машине не подтверждает все аудиодрайверы.

Для v0.1.2: автоматические тесты **149 passed**, frozen/public PCM и synthetic echo прошли. Пользователь отдельно подтвердил: **повторы ушли, свой голос сохранился**. Это одна ручная наблюдаемая проверка, без записанных численных измерений и длительности. Чистая Python-free/offline VM, долгий аппаратный сеанс и широкое качество KK/естественной смешанной речи остаются отдельными незавершёнными проверками. Подробности: [TESTING.md](TESTING.md), [release validation](verification/v0.1.2/release-validation.json), [manual acceptance](verification/v0.1.2/manual-acceptance.json).

### 11.3. Проверить source snapshot, если выпускаете отдельный source ZIP

Из корня исходников, с build venv:

```powershell
$taskSourceAudit = Join-Path $taskBuildRoot ("source-audit-" + [Guid]::NewGuid().ToString('N') + '.json')
& $taskPython ./build/source_package.py --root $taskSource --audit $taskSourceAudit
if ($LASTEXITCODE -ne 0) { throw 'Source audit не прошёл' }
```

Для source ZIP после завершения и ревью публичной документации:

```powershell
$taskSnapshotId = [Guid]::NewGuid().ToString('N')
$taskSnapshotAudit = Join-Path $taskBuildRoot ("source-final-" + $taskSnapshotId + '.json')
$taskSourceZip = Join-Path $taskReleaseOut ("GigaAM-Meeting-source-v0.1.2-" + $taskSnapshotId + '.zip')
& $taskPython ./build/source_package.py --root $taskSource --audit $taskSnapshotAudit --zip $taskSourceZip --finalized-docs
if ($LASTEXITCODE -ne 0) { throw 'Source packaging не прошло' }
Get-FileHash -LiteralPath $taskSourceZip -Algorithm SHA256
```

Перед будущей версией измените v0.1.2 в имени на её настоящий номер. Не упаковывайте репозиторий простым ZIP всего рабочего каталога: туда могут попасть venv, сырые pip reports, подписанные download URLs, устройства, локальные пути и пользовательские записи. GitHub автоматически предоставляет source archive тега; CI runtime workflow не создаёт дополнительный ручной source ZIP.

## 12. Сборка в GitHub Actions без публикации

Предпочтительный стандартный runner: `windows-2022`, Python `3.12.10 x64`, короткий venv `C:/gigaam-build/v`. Workflow: [windows.yml](../.github/workflows/windows.yml). Он запускается **вручную через workflow_dispatch**, а не автоматически на каждом push.

В интерфейсе GitHub: **Actions → Windows portable build → Run workflow → branch main → publish=false → Run workflow**. Дождитесь всех gates, скачайте artifact. Для публикации существующего v0.1.2 этот workflow повторно не запускайте.

Ниже CLI-эквивалент для сопровождающего с разрешённым GitHub CLI. Публичное скачивание приложения не требует авторизации; управление workflow требует прав репозитория.

### 12.1. Проверить GitHub CLI и авторизацию

```powershell
$taskRepo = 'AssylbekGizatov/gigaam-local-meeting-transcriber'
gh --version
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI не установлен' }
gh auth status
if ($LASTEXITCODE -ne 0) { throw 'Авторизуйтесь разрешённым способом, затем повторите' }
```

При необходимости выполните отдельно:

```powershell
gh auth login --web
```

Не копируйте токен в исходники, transcript, ZIP или скриншот. Workflow использует краткоживущий `github.token` с `contents: write`; приложение токенов не содержит. Если выбранный тип авторизации не поддерживает `gh run watch`, просматривайте run в Actions UI; не расширяйте права токена вслепую.

### 12.2. Запустить build-only

```powershell
@{ publish = $false; tag = 'v0.1.2' } | ConvertTo-Json -Compress | gh workflow run windows.yml --repo $taskRepo --ref main --json
if ($LASTEXITCODE -ne 0) { throw 'Workflow не запущен' }
```

Здесь `publish=false`: значение tag не создаёт и не меняет Release. Сохраните возвращённую ссылку run. Если команда не вернула run ID, найдите свой запуск:

```powershell
gh run list --repo $taskRepo --workflow windows.yml --limit 10 --json databaseId,headSha,status,conclusion,url
if ($LASTEXITCODE -ne 0) { throw 'Не удалось получить runs' }
$taskRunId = Read-Host 'ID именно вашего запуска (проверьте время и headSha)'
if ($taskRunId -notmatch '^[0-9]+$') { throw 'Некорректный run ID' }
gh run watch $taskRunId --repo $taskRepo --exit-status
if ($LASTEXITCODE -ne 0) { throw 'Run не прошёл; публикацию не продолжать' }
```

Не берите «последний run» автоматически при параллельной работе других людей. При ошибке:

```powershell
gh run view $taskRunId --repo $taskRepo --log-failed
```

Проверьте ошибки локально; сырые логи не публикуйте без проверки приватных данных.

### 12.3. Скачать artifact в новый каталог

```powershell
$taskArtifactDir = Join-Path $env:LOCALAPPDATA ("GigaAMMeeting/ci-artifacts/" + $taskRunId + "-" + [Guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $taskArtifactDir) { throw 'Каталог уже существует' }
gh run download $taskRunId --repo $taskRepo --name GigaAM-Meeting-windows-x64 --dir $taskArtifactDir
if ($LASTEXITCODE -ne 0) { throw 'Artifact не скачан' }
Get-ChildItem -LiteralPath $taskArtifactDir | Select-Object Name, Length
```

Это результат выбранного run, не автоматически официальный Release. Проверьте его собственный SHA256, манифест, DLL audit и provenance. Не применяйте к нему фиксированный checksum ранее опубликованного ZIP.

## 13. Выпустить следующую версию в GitHub Release

**Для изменения только документации новый бинарный Release не нужен.** README/памятка/уточнения отчёта в `main` могут быть новее документов внутри уже опубликованного ZIP. Существующие assets и теги не перезаписывайте.

Для новых изменений кода используйте новый номер, например **v0.1.3** ниже. Это **пример будущего выпуска**, а не утверждение, что такая версия уже существует.

### 13.1. Подготовить исходники следующего выпуска

В отдельном свежем checkout `main`:

1. Внести и проверить изменение.
2. Обновить `gigaam_meeting/__init__.py` (`__version__`), CHANGELOG, README и `docs/RELEASE.md`. Сверить [текущие файлы](../gigaam_meeting/__init__.py), не менять pinned model.
3. Обновить отчёт с фактическими проверками и оставшимися ограничениями.
4. Просмотреть diff на секреты, частные пути/аудио/meeting URLs; добавлять в Git только явно выбранные файлы.
5. Сделать commit и push с обычным review-процессом. `git add .` для рабочего каталога с отчётами не рекомендуется.
6. Выполнить build-only по разделу 12 и приёмку по разделу 11 для конкретного commit.

Для **выбранного checkout**, где уже сделаны ваши правки:

```powershell
git status --short
git diff --check
if ($LASTEXITCODE -ne 0) { throw 'Исправьте ошибки diff' }
git diff
```

После ревью явно перечислите изменённые файлы в `git add` — список зависит от правки. Затем:

```powershell
git diff --cached --check
if ($LASTEXITCODE -ne 0) { throw 'Staged diff не прошёл' }
git diff --cached
git commit -m "Prepare next verified Windows release"
if ($LASTEXITCODE -ne 0) { throw 'Commit не создан' }
git push origin main
if ($LASTEXITCODE -ne 0) { throw 'Push не прошёл' }
$taskPublishCommit = (git rev-parse HEAD).Trim()
```

Если main защищена, используйте разрешённый pull request процесс вместо прямого push; не обходите branch protection. В команды выше нельзя подставлять detached checkout v0.1.2 из раздела 9.

### 13.2. Убедиться, что новый tag и Release свободны

```powershell
$taskNextTag = 'v0.1.3'
$taskTagLines = @(git ls-remote --tags origin ("refs/tags/" + $taskNextTag))
if ($LASTEXITCODE -ne 0) { throw 'Не удалось проверить tags' }
if ($taskTagLines.Count -ne 0) { throw 'Tag уже существует; выберите новую версию' }
$taskExistingReleases = gh api --paginate ("repos/" + $taskRepo + "/releases") --jq '.[].tag_name'
if ($LASTEXITCODE -ne 0) { throw 'Нет достоверного списка Releases; не публикуйте' }
if (@($taskExistingReleases) -contains $taskNextTag) { throw 'Release уже существует' }
$taskRemoteMain = (gh api ("repos/" + $taskRepo + "/git/ref/heads/main") --jq '.object.sha').Trim()
if ($LASTEXITCODE -ne 0 -or $taskRemoteMain -ne $taskPublishCommit) { throw 'Main изменился: проверьте новый commit и повторите build-only' }
```

Workflow сам отказывается перезаписывать существующий Release. Проверка свободного tag здесь нужна дополнительно: не переиспользуйте уже существующий tag без Release.

### 13.3. Запустить публикацию нового pre-release

После успешной приёмки и проверки исходников:

```powershell
@{ publish = $true; tag = $taskNextTag } | ConvertTo-Json -Compress | gh workflow run windows.yml --repo $taskRepo --ref main --json
if ($LASTEXITCODE -ne 0) { throw 'Публикующий workflow не запущен' }
```

Не оставляйте default `v0.1.2` для нового выпуска. Публикующий workflow **заново собирает** программу, а не копирует build-only artifact; его checksum может отличаться. Сохраните новый run ID и дождитесь успеха командами раздела 12.2. Сверьте `headSha` с `$taskPublishCommit`: если main изменился перед dispatch, не принимайте этот выпуск как проверенный прежний commit.

### 13.4. Проверить опубликованный результат

```powershell
gh release view $taskNextTag --repo $taskRepo --json tagName,isPrerelease,url,assets,targetCommitish
if ($LASTEXITCODE -ne 0) { throw 'Release не найден или недоступен' }
```

Проверьте наличие четырёх runtime assets из раздела 1, соответствие tag/source commit/run, отметку pre-release и release notes. Повторите скачивание **из опубликованного Release**, SHA256, полную распаковку, манифест и self-test. Новый source ZIP, если нужен, создаётся отдельным проверенным шагом 11.3; не включайте build secrets.

Обновите `docs/verification/<version>/release-validation.json` только фактическими результатами: run URL, commit, размер/hash asset, проверки и ограничения. Сырой private evidence не переносите в публичные docs. Ошибка после публикации исправляется следующим номером версии; не заменяйте ZIP незаметно.

## 14. Типичные проблемы и правильное действие

| Симптом | Причина / действие |
| --- | --- |
| Отправлен source ZIP, EXE нет | Скачайте runtime `windows-x64.zip` |
| EXE не открывается / отсутствует DLL | Проверьте SHA256 и полную распаковку; не копируйте DLL с посторонних сайтов |
| Windows или корпоративная политика блокирует запуск | Остановитесь и обратитесь к администратору; не отключайте защиту и не обходите SmartScreen/политику |
| Нет модели / checksum model mismatch | Неполный или изменённый bundle; получите полный проверенный ZIP. Runtime не скачивает веса |
| Только свой голос, собеседники не слышны | Выход Teams/Zoom и выбранный loopback различаются; вручную выберите одно устройство |
| Нет сигнала в loopback при тишине | Само отсутствие пакетов при тишине не означает отключение драйвера; проверьте воспроизведение и уровни |
| Сменили гарнитуру / устройство исчезло | Stop, завершение текста, обновление списка, явный новый выбор и новый Start |
| Обработка отстаёт / видны пропуски | CPU не успевает, очередь ограничена; дождитесь завершения, для следующего сеанса уменьшите число источников, используйте подходящий CPU |
| Голос собеседника повторяется в микрофоне | Фильтр v0.1.2 требует совпадения текста/времени/PCM; включите перед сеансом, используйте наушники. Короткие/неуверенные совпадения могут остаться |
| Тихий свой ответ под громкими динамиками ухудшается | Ограничение акустики/ASR, полного AEC нет; наушники и отдельный тест тихой одновременной речи |
| «Повторный или устаревший блок» в старой 0.1.0 | WASAPI clock hotfix в 0.1.1+, используется и в 0.1.2; обновите полную сборку |
| Не сохраняется экспорт | Папка недоступна или имя занято; выберите разрешённую локальную папку/новое имя |
| Build HTTP 403 Microsoft notice | В v0.1.2 используется проверенный полный notice из репозитория; проверьте pinned файл. Не обходите vendor endpoint |
| Build reports уже существуют | Новый run/output каталог; не стирайте предыдущие доказательства ради повторного шага |
| Self-test прошёл, а реальный звук нет | Он не тестирует аудиодрайверы. Проведите уровни и ручную аппаратную приёмку |
| Аудит показывает 0 сети при ошибках доступа | Нулевой результат недостоверен; сначала подтвердите доступ к endpoint tables, зафиксируйте границы метода |
| Требуется работа Windows Service без пользователя | Текущая архитектура требует интерактивный сеанс и явный Start; такой deployment не поддерживается |

Приложение не пишет содержание разговора в диагностические логи. Для поддержки сначала соберите версию, этап ошибки, публичный release SHA, безопасный код ошибки и результат self-test. Не присылайте чужую запись, приватные meeting URLs, токены или сырые дампы без отдельного основания и согласия.

## 15. Что передать человеку сопровождения

Заполните эту карточку по фактам:

```text
Версия / tag:
Ссылка Release:
Source commit:
GitHub Actions run:
Имя runtime ZIP:
Размер / SHA256 ZIP:
Проверка полной распаковки и manifest:
Frozen self-test / дата:
Метод проверки сети и ограничения:
Windows / CPU / RAM (без username/hostname):
Ручные тесты: mic / loopback / вместе / наушники / динамики:
Пауза / Stop / новый сеанс / смена устройства:
Длительность фактического длительного теста:
RU / EN / KK / естественная смешанная речь:
Экспорт TXT / MD / DOCX / PDF:
Чистая Windows без Python и без сети: выполнено / не выполнено:
Папка данных и политика хранения (держать локально):
Предыдущая полная версия для отката:
Известные ограничения / ответственный:
```

К публичному отчёту прикладывайте только очищенные сведения и разрешённые public/synthetic fixtures. Пользовательские пути, устройства и политика хранения обычно остаются во внутренней карточке. Результат «проверено» означает действительно проведённый тест, а не наличие пункта в инструкции.

## 16. Связанные документы и источники команд

- [BUILD.md](BUILD.md) — подробности pipeline и разделения private/public evidence.
- [ARCHITECTURE.md](ARCHITECTURE.md) — устройство приложения.
- [PRIVACY.md](PRIVACY.md) — ограничения приватности и offline проверки.
- [ECHO.md](ECHO.md) — алгоритм и ограничения фильтра повторов.
- [TESTING.md](TESTING.md) — что проверено, а что осталось.
- [RELEASE.md](RELEASE.md) — release notes текущей версии.
- [GitHub CLI: workflow run](https://cli.github.com/manual/gh_workflow_run) — dispatch и JSON inputs.
- [GitHub CLI: run watch](https://cli.github.com/manual/gh_run_watch) — ожидание и exit status.
- [GitHub CLI: run download](https://cli.github.com/manual/gh_run_download) — скачивание artifact.
- [PowerShell Expand-Archive](https://learn.microsoft.com/powershell/module/microsoft.powershell.archive/expand-archive) и [Get-FileHash](https://learn.microsoft.com/powershell/module/microsoft.powershell.utility/get-filehash) — распаковка и SHA256.

Команды этой памятки сверены с текущими CLI-флагами приложения и сборочными скриптами; синтаксис PowerShell проверяется отдельно. Это не утверждение, что вся памятка повторно выполнена на чистой машине. Для конкретного развёртывания заполняйте карточку раздела 15.

