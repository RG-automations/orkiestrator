@echo off
cd /d "%~dp0"
python -m pip install -e . -q
rem Klucze trzymaj w pliku local.env (patrz local.env.example), nigdy w tym pliku.
if exist local.env for /f "usebackq eol=# tokens=1* delims==" %%A in ("local.env") do set "%%A=%%B"
if not "%ORKI_PROVIDER%"=="demo" if "%GEMINI_API_KEY%"=="" if "%GOOGLE_API_KEY%"=="" if "%ANTHROPIC_API_KEY%"=="" echo UWAGA: brak klucza AI. Skopiuj local.env.example do local.env i wpisz GEMINI_API_KEY (aistudio.google.com/api-keys), albo ustaw ORKI_PROVIDER=demo (dane fikcyjne, bez sieci).
python -m orkiestrator serve
pause
