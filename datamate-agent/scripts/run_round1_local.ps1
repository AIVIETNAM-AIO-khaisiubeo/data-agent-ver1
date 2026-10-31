param(
    [string]$DataPath = "",
    [string]$OutputPath = ".\submission.jsonl",
    [int]$Workers = 2,
    [int]$TimeBudgetSeconds = 86400
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if ([string]::IsNullOrWhiteSpace($DataPath)) {
    $DataPath = Join-Path $ProjectRoot "..\official_data\public_test"
}
if (-not (Test-Path $DataPath)) {
    throw "Public-test data directory not found: $DataPath"
}
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Project virtual-environment Python not found: $Python"
}
$DumpPath = ".\artifacts\round1_local_dump.jsonl"
$LogsPath = ".\artifacts\round1_local_logs"
New-Item -ItemType Directory -Force ".\artifacts" | Out-Null

& $Python -X utf8 ".\scripts\run.py" `
    --data $DataPath `
    --out $OutputPath `
    --dump $DumpPath `
    --logs $LogsPath `
    --set "llm.backend=none" `
    --set "agent.workers=$Workers" `
    --set "run.time_budget_s=$TimeBudgetSeconds" `
    --set "run.fast_after_s=$TimeBudgetSeconds" `
    --set "decision.calibrator_path=artifacts/calibrator_round1_offline.pkl" `
    --set "decision.offline_template_prior=0.75" `
    --set "decision.tau_answer=0.5" `
    --set "decision.tau_unanswerable=0.5"

if ($LASTEXITCODE -ne 0) {
    throw "DataMate execution failed with exit code $LASTEXITCODE"
}

& $Python -X utf8 ".\scripts\validate_submission.py" `
    --questions (Join-Path $DataPath "questions.jsonl") `
    --submission $OutputPath
if ($LASTEXITCODE -ne 0) {
    throw "Submission validation failed with exit code $LASTEXITCODE"
}

Write-Host "Round 1 submission is ready: $OutputPath"
