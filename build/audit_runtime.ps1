param([Parameter(Mandatory=$true)][string]$Exe, [Parameter(Mandatory=$true)][string]$ReportDirectory)
$ErrorActionPreference = 'Stop'
$testReport = Join-Path $ReportDirectory 'frozen-selftest.json'
$networkReport = Join-Path $ReportDirectory 'runtime-network-audit.json'
if ((Test-Path -LiteralPath $testReport) -or (Test-Path -LiteralPath $networkReport)) { throw 'Reports already exist; choose a new directory.' }
New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
$previousPath = $env:PATH
$previousPythonHome = $env:PYTHONHOME
$previousPythonPath = $env:PYTHONPATH
$samples = 0; $tcp = 0; $udp = 0
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:PYTHONHOME = ''; $env:PYTHONPATH = ''
    $runtimeProc = Start-Process -FilePath $Exe -ArgumentList @('--self-test', ('"' + $testReport + '"')) -PassThru -WindowStyle Hidden
    while (-not $runtimeProc.HasExited) {
        $samples++
        $tcp += @(Get-NetTCPConnection -OwningProcess $runtimeProc.Id -ErrorAction SilentlyContinue).Count
        $udp += @(Get-NetUDPEndpoint -OwningProcess $runtimeProc.Id -ErrorAction SilentlyContinue).Count
        Start-Sleep -Milliseconds 250
        $runtimeProc.Refresh()
    }
    $runtimeProc.WaitForExit()
    $exitCode = $runtimeProc.ExitCode
} finally {
    $env:PATH = $previousPath
    $env:PYTHONHOME = $previousPythonHome
    $env:PYTHONPATH = $previousPythonPath
}
$data = @{scope='PID TCP/UDP endpoint sampling during public fixture self-test; sanitized PATH/PYTHONHOME/PYTHONPATH'; snapshots=$samples; tcp_endpoint_observations=$tcp; udp_endpoint_observations=$udp; exit_code=$exitCode; clean_python_free_vm_tested=$false; physical_network_disconnection_tested=$false; limitations='Sampling can miss short connections/native activity routed via other processes. Python exists on this host but is excluded from child runtime search. No firewall/network settings changed.'}
$data | ConvertTo-Json | Set-Content -LiteralPath $networkReport -Encoding utf8
if ($exitCode -ne 0) { throw 'Frozen self-test failed; inspect its report.' }
if ($tcp -ne 0 -or $udp -ne 0) { throw 'Observed unexpected network endpoints; do not publish before review.' }
