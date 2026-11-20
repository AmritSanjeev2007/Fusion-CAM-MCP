# Links addin\FusionMCP into Fusion's AddIns folder (a junction, so edits in the repo are live).
# Usage:  powershell -ExecutionPolicy Bypass -File tools\install_addin.ps1 [-Uninstall]
param([switch]$Uninstall)

$repoAddin = Join-Path (Split-Path $PSScriptRoot -Parent) "addin\FusionMCP"
$addinsDir = Join-Path $env:APPDATA "Autodesk\Autodesk Fusion 360\API\AddIns"
$link      = Join-Path $addinsDir "FusionMCP"

if ($Uninstall) {
    if (Test-Path $link) {
        $item = Get-Item $link -Force
        if ($item.LinkType -eq "Junction") { $item.Delete(); "Removed junction $link" }
        else { "Not removing $link : it is a real folder, not this repo's junction." }
    } else { "Nothing to remove." }
    return
}

if (-not (Test-Path $addinsDir)) { throw "Fusion AddIns folder not found: $addinsDir (is Fusion installed?)" }
if (Test-Path $link) {
    $item = Get-Item $link -Force
    if ($item.LinkType -eq "Junction" -and $item.Target -contains $repoAddin) { "Already installed: $link -> $repoAddin"; return }
    throw "$link already exists and is not a junction to this repo. Remove it first."
}
New-Item -ItemType Junction -Path $link -Target $repoAddin | Out-Null
"Installed: $link -> $repoAddin"
"In Fusion: Shift+S > Add-Ins > FusionMCP > Run (tick 'Run on Startup')."
