@echo off
chcp 949 >nul
cd /d "%~dp0"

rem --- stop leftover app.py servers (ASCII only in this section) ---
rem A server started earlier keeps the old .py code in memory, so python edits
rem look ignored. On Windows two processes can also share port 5000 via
rem SO_REUSEADDR, and the older one answers. Clearing them first avoids both.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul

rem --- find a Python 3.8+ interpreter (ASCII only in this section) ---
set "PYCHK=import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)"
set "PYEXE="
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*") do if not defined PYEXE if exist "%%D\python.exe" "%%D\python.exe" -c "%PYCHK%" >nul 2>nul && set "PYEXE=%%D\python.exe"
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined PYEXE if exist "%%D\python.exe" "%%D\python.exe" -c "%PYCHK%" >nul 2>nul && set "PYEXE=%%D\python.exe"
for /d %%D in ("%ProgramFiles%\Python3*") do if not defined PYEXE if exist "%%D\python.exe" "%%D\python.exe" -c "%PYCHK%" >nul 2>nul && set "PYEXE=%%D\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\anaconda3\python.exe" "%USERPROFILE%\anaconda3\python.exe" -c "%PYCHK%" >nul 2>nul && set "PYEXE=%USERPROFILE%\anaconda3\python.exe"
if not defined PYEXE py -c "%PYCHK%" >nul 2>nul && set "PYEXE=py"

if defined PYEXE (
  echo ============================================
  echo   의대 예상문제 생성기 실행 중...
  echo   사용 Python: %PYEXE%
  echo   잠시 후 브라우저가 자동으로 열립니다.
  echo   (이 창을 닫으면 서버가 종료됩니다^)
  echo ============================================
  "%PYEXE%" app.py
  echo.
  echo 서버가 종료되었습니다. 문제가 있었다면 위 메시지를 확인하세요.
) else (
  echo [오류] Python 3.8 이상을 찾지 못했습니다.
  echo 처음이라면 "설치.bat" 을 먼저 실행하세요.
)

pause
