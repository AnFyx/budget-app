# start.ps1 - Lance l'app budget : charge le .env puis demarre l'interface.
#
# Usage : .\start.ps1   (depuis un terminal PowerShell, dans le dossier du projet)
#
# Si PowerShell bloque l'execution du script, lance plutot :
#   powershell -ExecutionPolicy Bypass -File .\start.ps1

# Se placer dans le dossier du script, pour pouvoir le lancer depuis n'importe ou.
Set-Location -Path $PSScriptRoot

# 1. Verifier la presence du .env
if (-not (Test-Path ".env")) {
    Write-Host "[ERREUR] Fichier .env introuvable dans $PSScriptRoot" -ForegroundColor Red
    Write-Host "Copie .env.example en .env et remplis-le, puis relance." -ForegroundColor Yellow
    exit 1
}

# 2. Charger le .env dans l'environnement du processus.
#    Ignore les lignes de commentaire (#) et les lignes vides.
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

# 4. Lancer l'app (run_app.py force l'ecoute sur 127.0.0.1).
Write-Host "Demarrage de l'app sur http://127.0.0.1:8501 (Ctrl+C pour arreter)..." -ForegroundColor Cyan
& $python run_app.py
