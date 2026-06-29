from datetime import datetime
import subprocess
import os

def should_be_running():
    now = datetime.now()
    current_minutes = now.hour * 60 + now.minute
    start_minutes   = 1 * 60        # 01:00
    stop_minutes    = 23 * 60 + 30  # 23:30
    return start_minutes <= current_minutes < stop_minutes

if should_be_running():
    print("[AutoStart] main.py start")
    os.chdir("/home/mlpc/Desktop/anprppp_with_time")
    subprocess.Popen(["/home/mlpc/miniconda3/envs/anpr/bin/python", "main_friday.py"])
else:
    print("[AutoStart] main.py not started (outside of scheduled time)")