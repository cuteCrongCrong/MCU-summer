@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem --- Python 실행기 찾기 (PATH에 py가 없어도 동작) ---
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE (
  if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
)
if not defined PYEXE (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" set "PYEXE=%%D\python.exe"
  )
)

if not defined PYEXE (
  echo [오류] Python을 찾지 못했습니다. Python을 먼저 설치하세요.
  pause
  exit /b 1
)

echo ============================================
echo   최초 1회 설정 - 필요한 패키지를 설치합니다
echo   사용 Python: %PYEXE%
echo   (flask, pymupdf, openai)
echo ============================================
"%PYEXE%" -m pip install --upgrade pip
"%PYEXE%" -m pip install flask pymupdf openai
echo.
echo 설치가 끝났습니다. 이제 "실행.bat" 을 더블클릭해 앱을 실행하세요.
pause
