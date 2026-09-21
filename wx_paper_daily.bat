@echo off
cd /d "E:\wyckoff-pyqt" && "D:\miniconda3\envs\wyckoff-pyqt\python.exe" -m wyckoff.paper_cron --scan >> "E:\wyckoff-pyqt\wx_paper_cron.log" 2>&1
