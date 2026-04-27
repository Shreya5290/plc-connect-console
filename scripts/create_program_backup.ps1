$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$backupDir = Join-Path $projectRoot 'backups'
$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$timestampedBackup = Join-Path $backupDir ("plc_connect_program_backup_{0}.zip" -f $timestamp)

$excludeNames = @('.git', 'backups', '__pycache__', '.venv', 'venv', 'env', 'db.sqlite3')

New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

$itemsToArchive = Get-ChildItem -LiteralPath $projectRoot -Force | Where-Object {
    $_.Name -notin $excludeNames
}

if (-not $itemsToArchive) {
    throw 'No project files found to back up.'
}

Compress-Archive -Path $itemsToArchive.FullName -DestinationPath $timestampedBackup -Force

Write-Output "Created: $timestampedBackup"
