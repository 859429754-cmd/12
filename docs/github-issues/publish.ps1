param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$gh = "gh"
if (-not (Get-Command $gh -ErrorAction SilentlyContinue)) {
    $candidate = "C:\Program Files\GitHub CLI\gh.exe"
    if (Test-Path $candidate) {
        $gh = $candidate
    }
}

& $gh auth status | Out-Host

$drafts = @(
    @{ Title = "PRD: AI Quant Trader stabilization and realism upgrade"; File = "000-prd-ai-quant-stabilization.md"; Label = "ready-for-human" },
    @{ Title = "Set up GitHub remote and issue publishing"; File = "001-github-remote-and-issues-setup.md"; Label = "ready-for-human" },
    @{ Title = "Implement FMZ-style pessimistic intrabar backtest ledger"; File = "002-backtest-intrabar-ledger.md"; Label = "ready-for-agent" },
    @{ Title = "Add console authentication before cloud exposure"; File = "003-console-auth-before-cloud.md"; Label = "ready-for-human" },
    @{ Title = "Rebuild news as fact-preserving timeline"; File = "004-news-timeline-fact-preserving.md"; Label = "ready-for-agent" },
    @{ Title = "Build walk-forward optimization harness"; File = "005-walk-forward-positive-optimization-harness.md"; Label = "ready-for-agent" }
)

foreach ($draft in $drafts) {
    $path = Join-Path $PSScriptRoot $draft.File
    if ($DryRun) {
        Write-Host "DRY RUN: gh issue create --title `"$($draft.Title)`" --body-file `"$path`" --label `"$($draft.Label)`""
        continue
    }
    & $gh issue create --title $draft.Title --body-file $path --label $draft.Label
}

