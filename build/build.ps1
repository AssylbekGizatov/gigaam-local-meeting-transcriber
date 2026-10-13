param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Output
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
Set-Location -LiteralPath $projectRoot
$runId = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')
$reportRoot = Join-Path $projectRoot ('build/reports/' + $runId)
$curatedEvidence = Join-Path $projectRoot ('build/release-evidence/' + $runId)
function Invoke-Python {
    & $Python @args
    if ($LASTEXITCODE -ne 0) { throw "Python step failed with exit code $LASTEXITCODE" }
}
New-Item -ItemType Directory -Path $reportRoot | Out-Null
Invoke-Python -m pip install -r requirements-runtime.txt -r requirements-build.txt --report (Join-Path $reportRoot 'dependencies.json')
Invoke-Python -m pip install torch==2.10.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cpu --report (Join-Path $reportRoot 'torch-acquisition.json')
Invoke-Python -m pip install --no-deps 'git+https://github.com/salute-developers/GigaAM.git@7447938d791c4f3e643386ee22c33777004293a5' --report (Join-Path $reportRoot 'gigaam-acquisition.json')
Invoke-Python -m pip check
Invoke-Python build/acquire_model.py
Invoke-Python build/patch_gigaam.py --output $reportRoot
Invoke-Python -m pytest tests -q --junitxml (Join-Path $reportRoot 'unit-tests.xml')
Invoke-Python -m ruff check gigaam_meeting app.py tests build
Invoke-Python -m PyInstaller --noconfirm --distpath dist --workpath build/pyinstaller GigaAM-Meeting.spec
$bundle = Join-Path $projectRoot 'dist/GigaAM-Meeting'
Invoke-Python build/verify_source.py --exe (Join-Path $bundle 'GigaAM-Meeting.exe') --report (Join-Path $reportRoot 'frozen-source-match.json')
foreach ($name in @('models','fonts','assets','docs')) {
    Copy-Item -LiteralPath $name -Destination (Join-Path $bundle $name) -Recurse -Force
}
Copy-Item -LiteralPath NOTICE.md -Destination $bundle
Copy-Item -LiteralPath fonts/LICENSE.txt -Destination (Join-Path $bundle 'DejaVu-font-LICENSE.txt')
Invoke-Python build/prepare_bundle.py --bundle $bundle --reports $reportRoot
& (Join-Path $PSScriptRoot 'audit_runtime.ps1') -Exe (Join-Path $bundle 'GigaAM-Meeting.exe') -ReportDirectory $reportRoot
Invoke-Python build/prepare_release_evidence.py --input $reportRoot --output $curatedEvidence --junit (Join-Path $reportRoot 'unit-tests.xml') --provenance $reportRoot --inventory (Join-Path $bundle 'dependency-inventory.json')
# Retain raw generated evidence privately; never merge it into public provenance.
$privateEvidenceRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot ('build/private-evidence/' + $runId)))
$expectedBuildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'build')) + [IO.Path]::DirectorySeparatorChar
$resolvedBundle = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $bundle).Path)
if ($resolvedBundle -ne [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist/GigaAM-Meeting')) -or -not $privateEvidenceRoot.StartsWith($expectedBuildRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Evidence paths escape the intended build workspace.' }
if ((Get-Item -LiteralPath $bundle).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Bundle must not be a reparse-point directory.' }
foreach ($folder in @('provenance', 'verification')) {
    $oldEvidencePath = [IO.Path]::GetFullPath((Join-Path $bundle $folder))
    if ($oldEvidencePath -ne (Join-Path $resolvedBundle $folder)) { throw 'Unexpected generated evidence path.' }
    if (Test-Path -LiteralPath $oldEvidencePath) {
        if ((Get-Item -LiteralPath $oldEvidencePath).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Generated evidence must not be a reparse-point directory.' }
        New-Item -ItemType Directory -Path $privateEvidenceRoot -Force | Out-Null
        Move-Item -LiteralPath $oldEvidencePath -Destination (Join-Path $privateEvidenceRoot $folder)
    }
}
Copy-Item -LiteralPath $curatedEvidence -Destination (Join-Path $bundle 'provenance') -Recurse
Copy-Item -LiteralPath (Join-Path $curatedEvidence 'dependency-inventory.json') -Destination (Join-Path $bundle 'dependency-inventory.json') -Force
Invoke-Python build/package.py --bundle $bundle --output $Output
