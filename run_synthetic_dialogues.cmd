@rem Запуск синтетических диалогов существующим Python-окружением deepagent.
@rem Вход: settings.py и сценарии. Выход: поток диалогов в терминале.
@rem Функций и подпрограмм нет. Аргумент --check запускает офлайн-проверку.
@echo off
chcp 65001 >nul
"%~dp0..\deepagent\.venv\Scripts\python.exe" -X utf8 -u "%~dp0run_synthetic_dialogues.py" %*
if errorlevel 1 echo Runner failed. See the error above.
pause
