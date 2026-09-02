[CmdletBinding()]
param(
    # Forwarded verbatim to `radarctl serve`, e.g. -RadarctlArgs --host,0.0.0.0,--port,8001
    [string[]]$RadarctlArgs = @()
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

# `security.secrets` deliberately reads only the real process environment (or
# the OS keyring), never `.env` or a config file (see that module's
# docstring) — so `radarctl serve` started without this step fails startup
# with `SecretLoadError: Missing required secret:
# COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_KEY.` even though `.env` has a
# value, because nothing has copied it into `os.environ` yet. This mirrors
# `demo_up.ps1`'s `Import-DotEnv`, kept here as a standalone entry point for
# a plain restart that doesn't need the rest of that bootstrap.
try {
    Import-DotEnv (Join-Path $repoRoot ".env")
    & (Join-Path $repoRoot ".venv\Scripts\python.exe") -m radarctl serve @RadarctlArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
