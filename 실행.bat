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
  echo [오류] Python을 찾지 못했습니다.
  echo 처음이라면 "설치.bat" 을 먼저 실행하세요.
  pause
  exit /b 1
)

echo ============================================
echo   의대 예상문제 생성기 실행 중...
echo   사용 Python: %PYEXE%
echo   잠시 후 브라우저가 자동으로 열립니다.
echo   (이 창을 닫으면 서버가 종료됩니다)
echo ============================================
"%PYEXE%" app.py
echo.
echo 서버가 종료되었습니다. 문제가 있었다면 위 메시지를 확인하세요.
pause
