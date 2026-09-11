# connect.ps1 - (Re)lance le consentement bancaire et recree la session chiffree.
#
# A utiliser quand le consentement a expire, OU quand le coffre n'est plus
# dechiffrable (passphrase perdue) : `connect` ECRASE le coffre avec une nouvelle
# session, chiffree avec la passphrase actuelle du .env. Les transactions deja en
# base (transactions.db) ne sont PAS touchees.
#
# Usage : .\connect.ps1   (depuis un terminal PowerShell, dans le dossier du projet)
# Si PowerShell bloque l'execution :
#   powershell -ExecutionPolicy Bypass -File .\connect.ps1

# Se placer dans le dossier du script.
Set-Location -Path $PSScriptRoot

# 1. Verifier la presence du .env
if (-not (Test-Path ".env")) {
    Write-Host "[ERREUR] Fichier .env introuvable dans $PSScriptRoot" -ForegroundColor Red
    Write-Host "Copie .env.example en .env et remplis-le, puis relance." -ForegroundColor Yellow
    exit 1
}

# 2. Charger le .env dans l'environnement du processus.
#    IDENTIQUE a start.ps1 : meme parsing => meme passphrase au connect et a l'app.
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process')
    }
}
Write-Host "[OK] .env charge." -ForegroundColor Green

# 3. Choisir l'interpreteur : le Python du venv s'il existe, sinon le global.
$venvPython = ".\.venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
} else {
    Write-Host "[AVERTISSEMENT] venv introuvable (.venv). Python global utilise." -ForegroundColor Yellow
    $python = "python"
}

# 4. Lancer le flux de consentement (ouvre le navigateur pour la BNP).
Write-Host "Lancement du consentement bancaire (une page va s'ouvrir)..." -ForegroundColor Cyan
& $python -m budget_poc.cli connect
