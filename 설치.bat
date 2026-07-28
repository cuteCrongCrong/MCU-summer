@echo off
chcp 949 >nul
cd /d "%~dp0"

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
  echo   최초 1회 설정 - 필요한 패키지를 설치합니다
  echo   사용 Python: %PYEXE%
  echo   (requirements.txt 기준: flask, pymupdf, openai, authlib, requests^)
  echo ============================================
  "%PYEXE%" -m pip install --upgrade pip
  "%PYEXE%" -m pip install -r requirements.txt
  echo.
  echo 설치가 끝났습니다. 이제 "실행.bat" 을 더블클릭해 앱을 실행하세요.
) else (
  echo [오류] Python 3.8 이상을 찾지 못했습니다.
  echo https://www.python.org 에서 최신 Python을 설치한 뒤 다시 실행하세요.
)

pause
