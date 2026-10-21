"""Where Vicky's own files live (.env, obk_ref_bundle.json, documents.xlsx, bim_index.db, results...).

Running from source: the project folder. Running as the packaged Vicky.exe: the folder next to the exe
(not PyInstaller's temporary unpack folder, which is deleted when the program closes).
"""
import os
import sys

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
# Files shipped inside the program (e.g. static/vicky_avatar.png)
RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
