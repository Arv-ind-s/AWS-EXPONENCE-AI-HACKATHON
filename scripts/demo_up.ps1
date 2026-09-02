[CmdletBinding()]
param(
    [string]$PythonPath = "python",
    [switch]$SkipInstall,
    # The base reference portfolio's own signal generator is unrelated to the
    # curated Phase 7A demo overlay, but its "sustained" evidence still feeds
    # the same forecast pressure term. At the default 365 days it produces
    # enough sustained evidence to force every borrower's covenant into the
    # `act` band regardless of the curated financials, so the demo bootstrap
    # defaults to a single day here -- long enough to seed `facility_conduct`
    # for the base portfolio, far short of the T3 sustained-evidence window
    # (14 days), so it never contaminates the curated borrowers' bands. Pass
    # a larger value only when you specifically need the full reference
    # portfolio's own signal history (e.g. evaluating it in isolation).
    [int]$SignalDays = 1,
    # Off by default: the bootstrap must never send a request to a paid gateway
    # just because the caller happens to have a key configured. Passing this
    # keeps whatever `.env` and the caller's environment select for the model
    # provider, so the demo exercises the real provider deliberately.
    [switch]$LiveModel
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Resolve from the script location so the command works from any current directory.
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path -Path $PSScriptRoot -ChildPath ".."))
Push-Location -LiteralPath $repoRoot

function Import-DotEnv {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#")) {
            continue
        }

        if ($trimmed -notmatch '^(?:export\s+)?(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$') {
            continue
        }

        $name = $Matches["name"]
        $value = $Matches["value"].Trim()
        if ([string]::IsNullOrWhiteSpace($value)) {
            # Blank entries in .env are documentation placeholders, not overrides
            # for the safe defaults in config/default.toml.
            continue
        }

        $isDoubleQuoted = $value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')
        $isSingleQuoted = $value.Length -ge 2 -and $value.StartsWith("'") -and $value.EndsWith("'")
        if ($isDoubleQuoted -or $isSingleQuoted) {
            $value = $value.Substring(1, $value.Length - 2)
        } else {
            # Support the common dotenv form `VALUE # comment` without treating
            # a hash inside a quoted value as a comment.
            $value = $value -replace '\s+#.*$', ''
        }
        if ([string]::IsNullOrWhiteSpace($value)) {
            continue
        }

        # A caller's non-empty environment takes precedence over .env. This keeps
        # CI and explicitly selected Python environments predictable.
        $existing = [Environment]::GetEnvironmentVariable($name, "Process")
        if ([string]::IsNullOrWhiteSpace($existing)) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function New-DemoSecret {
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes)
}

function Set-ProcessSecretIfMissing {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        [Environment]::SetEnvironmentVariable($Name, (New-DemoSecret), "Process")
    }
}

function Set-DemoEnvironment {
    [CmdletBinding()]
    param([bool]$UseLiveModel = $false)

    # The bootstrap is deliberately hermetic: it must never seed an external
    # database or send model requests just because the caller has those variables.
    $values = @{
        "COVENANT_RADAR_CONFIG" = ""
        "COVENANT_RADAR_DATABASE__URL" = "sqlite:///var/covenant-radar.db"
        "COVENANT_RADAR_SECURITY__SSO_PROVIDER" = "none"
        "COVENANT_RADAR_DOCUMENTS__STORE" = "local"
        "COVENANT_RADAR_DOCUMENTS__LOCAL_PATH" = "var/documents"
        "COVENANT_RADAR_NOTIFICATIONS__WEBHOOKS_ENABLED" = "false"
        "COVENANT_RADAR_OBSERVABILITY__METRICS_ENABLED" = "false"
        "COVENANT_RADAR_OBSERVABILITY__TRACING_ENABLED" = "false"
        "COVENANT_RADAR_WEB__HOST" = "127.0.0.1"
        "COVENANT_RADAR_WEB__PORT" = "8000"
        "COVENANT_RADAR_WEB__WORKERS" = "1"
    }
    if (-not $UseLiveModel) {
        # Replayed cassettes, not the configured gateway. These overwrite rather
        # than fill a gap, so `.env` cannot silently opt the demo into a live
        # provider; -LiveModel is the one way in, and it is explicit.
        $values["COVENANT_RADAR_AI__PROVIDER"] = "recorded"
        $values["COVENANT_RADAR_AI__MODEL"] = "demo-recorded"
        $values["COVENANT_RADAR_AI__RECORDED_RESPONSES_PATH"] = "evaluation/cassettes"
    }
    foreach ($entry in $values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }

    Set-ProcessSecretIfMissing "COVENANT_RADAR_SECURITY_SESSION_SECRET"
    Set-ProcessSecretIfMissing "COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_KEY"
    Set-ProcessSecretIfMissing "COVENANT_RADAR_SECURITY_CIN_FINGERPRINT_KEY"

    if ($UseLiveModel) {
        $provider = [Environment]::GetEnvironmentVariable("COVENANT_RADAR_AI__PROVIDER", "Process")
        if ([string]::IsNullOrWhiteSpace($provider) -or $provider -in @("none", "recorded")) {
            throw ("-LiveModel was passed but COVENANT_RADAR_AI__PROVIDER is '$provider'. " +
                   "Set a live provider, endpoint, model and COVENANT_RADAR_AI_API_KEY in .env first.")
        }
        Write-Host "Model provider: $provider (live, billable)" -ForegroundColor Yellow
    }
}

function Assert-PortFree {
    <#
        .SYNOPSIS
        Fail before seeding if the configured port is already taken.

        The seed and pipeline steps below take minutes.  Discovering a busy
        port only at the final `serve` step means paying that cost and then
        getting no server, which is exactly how a previous run left the
        repository: a stale listener held 8000, `serve` refused, and the demo
        looked like a static site because nothing had actually restarted.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$Port)

    $listener = $null
    try {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    } catch {
        return
    }
    if ($null -ne $listener) {
        $owner = ($listener | Select-Object -First 1).OwningProcess
        $name = try { (Get-Process -Id $owner -ErrorAction Stop).ProcessName } catch { "unknown" }
        throw ("Port $Port is already in use by PID $owner ($name). " +
               "Stop that process (or set COVENANT_RADAR_WEB__PORT) and run this script again.")
    }
}

function Invoke-Checked {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    Write-Host "`n== $Label ==" -ForegroundColor Cyan
    & $Command
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$Label failed with exit code $exitCode."
    }
}

try {
    Import-DotEnv (Join-Path $repoRoot ".env")
    Set-DemoEnvironment -UseLiveModel:$LiveModel.IsPresent

    $pythonCommands = @(Get-Command -Name $PythonPath -CommandType Application -ErrorAction SilentlyContinue)
    if ($pythonCommands.Count -eq 0) {
        throw "Python 3.12 or newer is required, but '$PythonPath' was not found on PATH."
    }
    $python = $pythonCommands[0].Source
    $versionOutput = & $python --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to execute '$python'."
    }
    $versionMatch = [regex]::Match(($versionOutput -join " "), 'Python\s+(?<major>\d+)\.(?<minor>\d+)')
    if (-not $versionMatch.Success -or
        [int]$versionMatch.Groups["major"].Value -ne 3 -or
        [int]$versionMatch.Groups["minor"].Value -lt 12) {
        throw "Python 3.12 or newer is required; found '$($versionOutput -join ' ')'."
    }

    New-Item -ItemType Directory -Force -Path "var", "var/documents" | Out-Null

    $port = [int][Environment]::GetEnvironmentVariable("COVENANT_RADAR_WEB__PORT", "Process")
    Assert-PortFree -Port $port

    if (-not $SkipInstall) {
        Invoke-Checked "Install Covenant Radar" { & $python -m pip install -e . }
    } else {
        Write-Host "`n== Install Covenant Radar ==" -ForegroundColor Cyan
        Write-Host "Skipped by -SkipInstall." -ForegroundColor DarkGray
    }
    Invoke-Checked "Apply database migrations" { & $python -m radarctl migrate upgrade }
    Invoke-Checked "Load reference portfolio" {
        # Each borrower-day generates seven signal events. The portfolio is
        # sized to the curated 24-company demo roster (one borrower per
        # industry), so even the full 365-day default is a few thousand rows
        # here -- -SignalDays exists to keep the base portfolio's signal
        # history short enough that it can never become "sustained" evidence
        # (see the -SignalDays default above), not for row-count size.
        if ($SignalDays -gt 0) {
            & $python -m radarctl seed --reference-portfolio --signal-days $SignalDays
        } else {
            & $python -m radarctl seed --reference-portfolio
        }
    }
    Invoke-Checked "Load Phase 7A demo data" { & $python -m radarctl seed --demo-covenants }
    Invoke-Checked "Create demo personas" { & $python create_user.py }
    # Personas are created before the pipeline so its notification dispatch has
    # recipients to resolve; a pipeline run against a user-less database
    # silently produces an empty notification table.
    Invoke-Checked "Run the real nightly pipeline" { & $python -m radarctl job run nightly.pipeline }

    Write-Host "`nCovenant Radar is ready at http://127.0.0.1:$port" -ForegroundColor Green
    Write-Host "Sign in with any spec section 7 persona, password CovenantRadar#2026:" -ForegroundColor Green
    Write-Host "  riskhead - portfolio-wide risk view, simulator, memos, overrides" -ForegroundColor Green
    Write-Host "  credit / approver - covenant intake and maker-checker approval" -ForegroundColor Green
    Write-Host "  rm - assigned portfolio only    auditor - read-only audit trail" -ForegroundColor Green
    Write-Host "  admin - users, jobs, connectors  steward - quarantine and corrections" -ForegroundColor Green
    & $python -m radarctl serve
    $serverExitCode = $LASTEXITCODE
    if ($serverExitCode -ne 0) {
        throw "Covenant Radar server stopped with exit code $serverExitCode."
    }
} finally {
    Pop-Location
}
