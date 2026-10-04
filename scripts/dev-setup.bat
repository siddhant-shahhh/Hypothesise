@echo off
:: Thin cmd.exe launcher for dev-setup.ps1
:: Usage: scripts\dev-setup.bat [--here]
::
:: If PowerShell complains about execution policy, run once in PowerShell:
::   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev-setup.ps1" %*
