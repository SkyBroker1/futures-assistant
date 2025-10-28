@echo off
setlocal enableextensions

rem === Налаштування ===
set REPO=SkyBroker1/futures-assistant
set WORKFLOW_FILE=.github/workflows/run_futures_assistant.yml
set BRANCH=main

for /f "tokens=1-4 delims=.-/ " %%a in ("%date%") do ( set DD=%%a & set MM=%%b & set YYYY=%%c )
for /f "tokens=1-2 delims=: " %%h in ("%time%") do ( set HH=%%h & set MIN=%%i )
set TS=%YYYY%%MM%%DD%_%HH%%MIN%

if not exist out_local mkdir out_local
if not exist artifacts mkdir artifacts

echo ▶ 1) Стартуємо хмарний workflow...
gh workflow run "%WORKFLOW_FILE%" -R "%REPO%" --ref %BRANCH%

echo ▶ 2) Локальний збір...
python scripts\collect_local.py --out out_local
if errorlevel 1 (
  echo ❌ Локальний збір впав
  goto download_cloud
)

echo ▶ 3) Публікуємо локальні JSON як Release-асети...
set TAG=local-%TS%
gh release create %TAG% -R "%REPO%" -t "local %TS%" -n "Локальні JSON %TS%"
for %%f in (out_local\*.json) do gh release upload %TAG% "%%f" -R "%REPO%" --clobber

:download_cloud
echo ▶ 4) Чекаємо хмарний ран та качаємо артефакти...
timeout /t 90 >NUL
gh run download -R "%REPO%" --name out_cloud --dir artifacts --latest
gh run download -R "%REPO%" --name logs_cloud --dir artifacts --latest

echo ✅ Готово.
echo - Локальні JSON: out_local\
echo - Реліз: %TAG% у %REPO%
echo - Хмарні артефакти: artifacts\
endlocal
