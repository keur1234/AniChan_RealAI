#!/usr/bin/env python3
"""Update the BIM DB that Vicky searches.

1. Download the BIM Excel files from the Google Drive folder into bim_files/ (only new or changed files)
2. Read column OBK.FM_25Digits_TAG (sheet Combined_25Digits; CI_ files: sheets P01,P02,P03,P04,P06) into bim_index.db

Run it whenever you like (the bot also does it every week if BIM_DRIVE_FOLDER is set in .env):

    python update_bim_db.py                       # download from the Drive folder in .env, then build the DB
    python update_bim_db.py --drive <folder link> # use this Drive folder
    python update_bim_db.py --folder D:\\BIM       # skip downloading; build from Excel files already on disk
    python update_bim_db.py --full                # re-download and re-read every file

The bot can stay running while this runs; it sees the new data on the next question.
"""
import argparse
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()


def main():
    ap = argparse.ArgumentParser(description="Download the BIM Excel files and build the BIM DB")
    ap.add_argument("--drive", help="Google Drive folder link (default: BIM_DRIVE_FOLDER in .env)")
    ap.add_argument("--folder", help="build from Excel files in this folder instead of downloading")
    ap.add_argument("--download-dir", help="where downloaded files go (default: bim_files next to this script)")
    ap.add_argument("--db", help="DB file (default: bim_index.db next to this script)")
    ap.add_argument("--full", action="store_true", help="re-download and re-read every file")
    args = ap.parse_args()

    # bim_index reads its settings from the environment when it is imported
    if args.folder:
        os.environ["BIM_LOCAL_FOLDER"] = args.folder
        os.environ["BIM_DRIVE_FOLDER"] = ""
    elif args.drive:
        os.environ["BIM_DRIVE_FOLDER"] = args.drive
        os.environ["BIM_LOCAL_FOLDER"] = ""
    if args.download_dir:
        os.environ["BIM_DOWNLOAD_DIR"] = args.download_dir
    if args.db:
        os.environ["BIM_DB"] = args.db

    import bim_index
    if not bim_index.configured():
        sys.exit("No source: pass --drive <Drive folder link> or --folder <local folder>, "
                 "or set BIM_DRIVE_FOLDER in .env")

    print(f"Source : {bim_index.DRIVE_FOLDER or bim_index.LOCAL_FOLDER}")
    if bim_index.DRIVE_FOLDER:
        print(f"Files  : {bim_index.DOWNLOAD_DIR}")
    print(f"DB     : {bim_index.DB_PATH}")
    rules = "".join(f"; {p}: sheets {', '.join(sh)}" for p, sh in bim_index.SHEET_RULES)
    print(f"Reads  : column {bim_index.TAG_COLUMN}, sheet {bim_index.SHEET}{rules}\n")

    started = time.time()
    report = bim_index.sync(force=args.full, progress=lambda msg: print("  " + msg, flush=True))
    print(f"\n{report}\nDone in {time.time() - started:.0f}s\n")
    print(bim_index.format_status("en"))


if __name__ == "__main__":
    main()
