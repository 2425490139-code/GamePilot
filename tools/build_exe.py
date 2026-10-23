from pathlib import Path
import subprocess, sys
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'gamepilot'
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed',
 '--name','GamePilot','--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build'),'--specpath',str(ROOT/'build'),
 '--icon',str(APP/'assets/gamepilot.ico'),'--version-file',str(APP/'assets/windows_version.txt'),
 '--add-data',f"{APP/'assets'};assets",'--add-data',f"{APP/'templates'};templates",
 '--add-data',f"{APP/'tools/ocr.ps1'};tools",'--add-data',f"{APP/'tools/ocr_server.ps1'};tools",
 str(APP/'main.py')],cwd=ROOT,check=True)
