# Creates a desktop shortcut + a Start-menu entry for IFU Artwork.
# Run once: right-click this file -> "Run with PowerShell".
# Idempotent: re-running overwrites the existing shortcuts.

$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$bat = Join-Path $here "Start IFU Artwork.bat"
if (-not (Test-Path $bat)) {
    Write-Error "Can't find 'Start IFU Artwork.bat' at $bat"
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$startMenu = [Environment]::GetFolderPath("Programs")

function Make-Shortcut($lnkPath) {
    $wsh = New-Object -ComObject WScript.Shell
    $sc = $wsh.CreateShortcut($lnkPath)
    $sc.TargetPath = $bat
    $sc.WorkingDirectory = $here
    $sc.Description = "Launch the IFU Artwork generator"
    $sc.WindowStyle = 1  # normal
    # Use a built-in Windows icon (laptop with image) -- substitutes a
    # blank document icon if you'd rather have something neutral.
    $sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,76"
    $sc.Save()
    Write-Host "  created  $lnkPath"
}

Make-Shortcut (Join-Path $desktop "IFU Artwork.lnk")
Make-Shortcut (Join-Path $startMenu "IFU Artwork.lnk")

Write-Host ""
Write-Host "Done.  Double-click 'IFU Artwork' on your desktop, or"
Write-Host "type 'IFU Artwork' in the Start menu, to launch the tool."
