@echo off
REM Run the blinds-controller on Python 3.13 (madmom + PyTorch CUDA work there).
py -3.13 "%~dp0blinds_controller.py" %*
