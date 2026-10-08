"""Build the standalone Rocket Radar app for the current OS:  python build.py

Output: dist/RocketRadar (dist/RocketRadar.exe on Windows). Needs `pip install pyinstaller`.
PyInstaller can't cross-compile, so build on each OS (the GitHub Actions workflow does this).
"""
import os
import sys

import PyInstaller.__main__

here = os.path.dirname(os.path.abspath(__file__))
PyInstaller.__main__.run([
    os.path.join(here, "app.py"),
    "--name", "RocketRadar",
    "--onefile",
    "--console",  # the window shows status and keeps the app running
    "--add-data", f"{os.path.join(here, 'static')}{os.pathsep}static",
    "--hidden-import", "websockets",
    "--distpath", os.path.join(here, "dist"),
    "--workpath", os.path.join(here, "build"),
    "--specpath", os.path.join(here, "build"),
    "--noconfirm",
    "--clean",
])
print(f"\nBuilt: {os.path.join(here, 'dist', 'RocketRadar' + ('.exe' if sys.platform == 'win32' else ''))}")
