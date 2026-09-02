<#
    .SYNOPSIS
    Export the machine's trusted CA certificates as a PEM bundle.

    .DESCRIPTION
    Python does not consult the Windows certificate store; httpx trusts the
    public root list that ships with `certifi` and nothing else.  On a network
    that inspects TLS, the model gateway presents a chain signed by the
    organisation's own CA, which is in the Windows store and absent from that
    list, so every provider call fails with CERTIFICATE_VERIFY_FAILED before a
    request is ever sent.

    This script writes those anchors to a PEM file that
    `COVENANT_RADAR_AI__CA_BUNDLE` can name.  The bundle is loaded *in addition
    to* the public roots, never instead of them, so trusting an internal CA
    does not stop the public web from verifying.

    The output is machine-specific and belongs under `var/`, which is not
    tracked.  Re-run it when the organisation rotates its CA.

    .EXAMPLE
    pwsh scripts/export_ca_bundle.ps1
    # then, in .env:  COVENANT_RADAR_AI__CA_BUNDLE=var/corporate-ca.pem
#>
[CmdletBinding()]
param(
    [string]$OutputPath = "var/corporate-ca.pem"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path -Path $PSScriptRoot -ChildPath ".."))
Push-Location -LiteralPath $repoRoot

try {
    if (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
        $OutputPath = Join-Path $repoRoot $OutputPath
    }
    $outputDirectory = Split-Path -Parent $OutputPath
    if (-not (Test-Path -LiteralPath $outputDirectory)) {
        New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
    }

    # Root holds trust anchors; CA holds the intermediates a server may fail to
    # send. OpenSSL needs a complete path to an anchor, so both are exported.
    $stores = @(
        "Cert:\LocalMachine\Root",
        "Cert:\LocalMachine\CA",
        "Cert:\CurrentUser\Root",
        "Cert:\CurrentUser\CA"
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $seen = New-Object System.Collections.Generic.HashSet[string]
    $now = Get-Date
    $expired = 0

    foreach ($store in $stores) {
        $certificates = @(Get-ChildItem -Path $store -ErrorAction SilentlyContinue)
        foreach ($certificate in $certificates) {
            if ($certificate.NotAfter -lt $now -or $certificate.NotBefore -gt $now) {
                $expired++
                continue
            }
            if (-not $seen.Add($certificate.Thumbprint)) {
                continue
            }
            $lines.Add("# $($certificate.Subject)")
            $lines.Add("-----BEGIN CERTIFICATE-----")
            $lines.Add([Convert]::ToBase64String($certificate.RawData, 'InsertLineBreaks'))
            $lines.Add("-----END CERTIFICATE-----")
        }
    }

    if ($seen.Count -eq 0) {
        throw "No usable certificates were found in the Windows trust store."
    }

    Set-Content -LiteralPath $OutputPath -Value $lines -Encoding ascii

    Write-Host "Wrote $($seen.Count) certificate(s) to $OutputPath" -ForegroundColor Green
    if ($expired -gt 0) {
        Write-Host "Skipped $expired certificate(s) outside their validity window." -ForegroundColor DarkGray
    }
    # Windows PowerShell 5.1 targets .NET Framework, which has no
    # Path.GetRelativePath, so the repository prefix is trimmed by hand.
    $relative = $OutputPath
    if ($relative.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $relative = $relative.Substring($repoRoot.Length).TrimStart('\', '/')
    }
    $relative = $relative.Replace('\', '/')
    Write-Host "Set COVENANT_RADAR_AI__CA_BUNDLE=$relative to use it." -ForegroundColor Green
} finally {
    Pop-Location
}
