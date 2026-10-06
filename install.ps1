# Adds a "Steam (lite)" Start Menu shortcut (no console window) and puts
# `steamlite` on the user PATH.
# Run with: powershell -ExecutionPolicy Bypass -File install.ps1
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { Write-Error "Python 3 not found: install it from python.org (check 'Add to PATH')"; exit 1 }

$steamExe = (Get-ItemProperty "HKCU:\Software\Valve\Steam" -ErrorAction SilentlyContinue).SteamExe
$lnkPath = Join-Path ([Environment]::GetFolderPath("Programs")) "Steam (lite).lnk"
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$dir\steamlite.py`" launch --freeze"
$lnk.WorkingDirectory = $dir
if ($steamExe) { $lnk.IconLocation = $steamExe.Replace("/", "\") }
$lnk.Save()

$path = [Environment]::GetEnvironmentVariable("Path", "User")
if ($path -notlike "*$dir*") {
    [Environment]::SetEnvironmentVariable("Path", "$path;$dir", "User")
}
Write-Host "installed: open 'Steam (lite)' from the Start Menu, or run 'steamlite launch' in a new terminal"
Write-Host "log file: $env:LOCALAPPDATA\steamlite\steamlite.log"
