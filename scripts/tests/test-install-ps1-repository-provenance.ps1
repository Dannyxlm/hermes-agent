# Unit tests for install.ps1's GitHub archive URL resolver.
#
# Run from a PowerShell prompt:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/tests/test-install-ps1-repository-provenance.ps1
#
# We execute only the pure helper extracted through the PowerShell AST so the
# installer's top-level body never runs.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installScript = Join-Path $repoRoot "scripts/install.ps1"

if (-not (Test-Path $installScript)) {
    throw "Could not locate install.ps1 at $installScript"
}

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($installScript, [ref]$tokens, [ref]$errors)

if ($errors.Count -gt 0) {
    throw "install.ps1 has parse errors: $($errors -join '; ')"
}

$fnAst = $ast.FindAll(
    {
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq "Get-GitHubArchiveUrl"
    },
    $true
) | Select-Object -First 1

if (-not $fnAst) {
    throw "Get-GitHubArchiveUrl not found in install.ps1"
}

. ([scriptblock]::Create($fnAst.Extent.Text))

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Expected -ne $Actual) {
        throw "FAIL: $Label expected '$Expected', got '$Actual'"
    }

    Write-Host "OK: $Label" -ForegroundColor Green
}

$repository = "Dannyxlm/hermes-agent"

Assert-Equal `
    -Expected "https://github.com/$repository/archive/abc123.zip" `
    -Actual (Get-GitHubArchiveUrl -Repository $repository -Commit "abc123" -Branch "main") `
    -Label "commit archive uses declared producer"

Assert-Equal `
    -Expected "https://github.com/$repository/archive/refs/tags/v1.2.3.zip" `
    -Actual (Get-GitHubArchiveUrl -Repository $repository -Tag "v1.2.3" -Branch "main") `
    -Label "tag archive uses declared producer"

Assert-Equal `
    -Expected "https://github.com/$repository/archive/refs/heads/release.zip" `
    -Actual (Get-GitHubArchiveUrl -Repository $repository -Branch "release") `
    -Label "branch archive uses declared producer"

Write-Host "All install.ps1 repository provenance tests passed."
