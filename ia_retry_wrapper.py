#!/usr/bin/env python3
"""
IA Retry Wrapper
================

Runs `ia upload --spreadsheet` (or `ia metadata --spreadsheet`) against a CSV
and keeps retrying until the run finishes without crashing, so a flaky network
connection or a slow Internet Archive server doesn't need babysitting.

- Prompts for the CSV (or takes it as an argument).
- Asks whether the batch is metadata-only (adds --no-derive) or includes
  assets/media files (derive runs as normal).
- If the CSV has no `file` column, asks whether this is a metadata update
  (runs `ia metadata --spreadsheet`) or an incomplete sheet (stops).
- Writes a log named after the CSV, in the same folder as the CSV.
- Keeps the computer awake while it runs (macOS, Windows, and most Linux).

Why not just check ia's exit code? In spreadsheet mode, `ia upload --checksum`
counts files it *skipped* (already uploaded) as errors, so a finished re-run
still exits with a failure code. This wrapper reads the output instead:
a Python "Traceback" means ia crashed and the run is retried; "error uploading"
lines mean individual files failed and get another pass.

Works on macOS, Windows and Linux with Python 3.8+ and the `internetarchive`
package installed and configured (`ia configure`).
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

__version__ = "1.0.0"

CRASH_MARKER = b"Traceback (most recent call last)"
FILE_ERROR_RE = re.compile(rb" error uploading (.+?): ")
IDENTIFIER_COLUMNS = ("identifier", "item")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask_choice(question: str, options: dict[str, str]) -> str:
    """Ask a numbered question until a valid option is typed. Returns the key."""
    say(question)
    for key, label in options.items():
        say(f"  {key}) {label}")
    while True:
        try:
            answer = input("> ").strip()
        except EOFError:
            sys.exit("\nNo answer given; stopping.")
        if answer in options:
            return answer
        say(f"Please type one of: {', '.join(options)}")


def ask_yes_no(question: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def clean_dropped_path(raw: str) -> str:
    """Tidy a path that was typed, pasted, or dragged into the terminal.

    Handles surrounding quotes (Windows, Linux), PowerShell's leading `& `,
    macOS Terminal's backslash-escaped spaces, and `~`.
    """
    p = raw.strip()
    if p.startswith("& "):  # PowerShell drag-and-drop
        p = p[2:].strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "'\"":
        p = p[1:-1]
    if os.name != "nt":
        p = re.sub(r"\\(.)", r"\1", p)  # "My\ Folder" -> "My Folder"
    return os.path.expanduser(p)


def prompt_for_csv() -> Path:
    while True:
        try:
            raw = input("Path to the upload CSV (you can drag the file in here): ")
        except EOFError:
            sys.exit("\nNo CSV given; stopping.")
        if not raw.strip():
            continue
        path = Path(clean_dropped_path(raw))
        if path.is_file():
            return path.resolve()
        say(f"Can't find a file at: {path}")


def find_ia() -> list[str]:
    exe = shutil.which("ia")
    if exe:
        return [exe]
    sys.exit(
        "Couldn't find the `ia` command.\n"
        "Install it with:  pip install internetarchive\n"
        "then run:         ia configure"
    )


# --------------------------------------------------------------------------
# CSV checks
# --------------------------------------------------------------------------

class SheetInfo:
    def __init__(self, path: Path):
        self.path = path
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            self.columns = [c for c in (reader.fieldnames or [])]
            self.rows = list(reader)

    @property
    def has_file_column(self) -> bool:
        return "file" in self.columns

    @property
    def identifier_column(self) -> str | None:
        for name in IDENTIFIER_COLUMNS:
            if name in self.columns:
                return name
        return None

    def blank_file_rows(self) -> list[int]:
        """Spreadsheet row numbers (header = row 1) with an empty `file` cell."""
        return [i + 2 for i, r in enumerate(self.rows) if not (r.get("file") or "").strip()]

    def item_count(self) -> int:
        col = self.identifier_column
        if not col:
            return 0
        return len({(r.get(col) or "").strip() for r in self.rows} - {""})

    def missing_local_files(self, base: Path) -> list[str]:
        """Files named in the sheet that don't exist when resolved from `base`."""
        missing = []
        for r in self.rows:
            f = (r.get("file") or "").strip()
            if f and not (base / os.path.expanduser(f)).exists():
                missing.append(f)
        return missing


# --------------------------------------------------------------------------
# Keep the computer awake
# --------------------------------------------------------------------------

class KeepAwake:
    """Best-effort: stop the computer idling to sleep while uploads run."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.windows = False

    def __enter__(self) -> "KeepAwake":
        system = platform.system()
        try:
            if system == "Darwin" and shutil.which("caffeinate"):
                self.proc = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            elif system == "Windows":
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                self.windows = True
            elif system == "Linux" and shutil.which("systemd-inhibit"):
                self.proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle:sleep", "--who=IA Retry Wrapper",
                     "--why=Uploading to the Internet Archive", "--mode=block",
                     "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass  # staying awake is a nicety, never a reason to fail
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self.windows:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass


# --------------------------------------------------------------------------
# Running ia
# --------------------------------------------------------------------------

def stamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_once(cmd: list[str], log, cwd: Path) -> tuple[int, bool, set[str]]:
    """Run ia, echoing output to the screen and the log.

    Returns (exit code, crashed?, set of files that reported upload errors).
    """
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, cwd=str(cwd))
    out = sys.stdout.buffer
    crashed = False
    failed: set[str] = set()
    pending = b""  # unfinished last line, so text split across chunks is still matched

    def scan(line: bytes) -> None:
        m = FILE_ERROR_RE.search(line)
        if m:
            failed.add(m.group(1).decode("utf-8", "replace"))

    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(8192)
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            log.write(chunk)
            pending += chunk
            if CRASH_MARKER in pending:
                crashed = True
            *lines, pending = pending.split(b"\n")
            for line in lines:
                scan(line)
            pending = pending[-4096:]  # progress bars redraw with \r and never end a line
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    scan(pending)
    log.flush()
    return code, crashed, failed


def build_command(ia: list[str], mode: str, csv_path: Path, retries: int) -> list[str]:
    if mode == "metadata-update":
        return ia + ["metadata", f"--spreadsheet={csv_path}"]
    cmd = ia + ["upload", f"--spreadsheet={csv_path}", "--retries", str(retries), "--checksum"]
    if mode == "metadata-only":
        cmd.append("--no-derive")
    return cmd


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

MODE_LABELS = {
    "metadata-only": "Metadata-only upload (--no-derive)",
    "assets": "Asset upload (derive runs)",
    "metadata-update": "Metadata update, no files (ia metadata --spreadsheet)",
}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ia-retry",
        description="Keep re-running an Internet Archive spreadsheet upload until it finishes.",
    )
    p.add_argument("csv", nargs="?", help="Path to the upload CSV. You'll be asked if it's left out.")
    p.add_argument("--mode", choices=list(MODE_LABELS),
                   help="Skip the question: metadata-only, assets, or metadata-update.")
    p.add_argument("--max-attempts", type=int, default=20,
                   help="Give up after this many runs (default: 20).")
    p.add_argument("--wait", type=int, default=120,
                   help="Seconds to wait before retrying after a crash (default: 120).")
    p.add_argument("--retries", type=int, default=10,
                   help="Passed to ia upload --retries (default: 10).")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Don't ask for confirmation before starting.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def choose_mode(sheet: SheetInfo, preset: str | None) -> str:
    if not sheet.has_file_column:
        say("This CSV has no `file` column.")
        if preset == "metadata-update":
            return preset
        choice = ask_choice(
            "Is this:",
            {"1": "A metadata update (change metadata on items already on the Archive)",
             "2": "An incomplete sheet (the file column should be there)"},
        )
        if choice == "1":
            return "metadata-update"
        sys.exit("Stopping. Add a `file` column listing each file to upload, then run this again.")

    if preset == "metadata-update":
        sys.exit("--mode metadata-update is for sheets without a `file` column; this one has one.")
    if preset:
        return preset
    choice = ask_choice(
        "What kind of upload is this?",
        {"1": "Metadata only (adds --no-derive)",
         "2": "Includes assets / media files (derive runs as normal)"},
    )
    return "metadata-only" if choice == "1" else "assets"


def main(argv=None) -> int:
    args = parse_args(argv)
    say(f"IA Retry Wrapper {__version__}\n")

    csv_path = Path(clean_dropped_path(args.csv)).resolve() if args.csv else prompt_for_csv()
    if not csv_path.is_file():
        sys.exit(f"Can't find a file at: {csv_path}")

    try:
        sheet = SheetInfo(csv_path)
    except (UnicodeDecodeError, csv.Error) as exc:
        sys.exit(f"Couldn't read the CSV ({exc}). Is it saved as CSV UTF-8?")

    if not sheet.rows:
        sys.exit("The CSV has a header but no rows.")
    if not sheet.identifier_column:
        sys.exit("The CSV has no `identifier` column. Add one, then run this again.")

    mode = choose_mode(sheet, args.mode)
    run_dir = Path.cwd()

    # Sanity checks that would otherwise crash ia and loop forever.
    if mode != "metadata-update":
        blanks = sheet.blank_file_rows()
        if blanks:
            shown = ", ".join(map(str, blanks[:15])) + (" ..." if len(blanks) > 15 else "")
            say(f"\nWarning: {len(blanks)} row(s) have an empty `file` cell (rows {shown}).")
            if not ask_yes_no("Continue anyway?"):
                return 1
        missing = sheet.missing_local_files(Path.cwd())
        if missing and not sheet.missing_local_files(csv_path.parent):
            # Paths are relative to the CSV's folder, so run ia from there.
            run_dir = csv_path.parent
            missing = []
        if missing:
            say(f"\nWarning: {len(missing)} file(s) listed in the CSV weren't found from this folder:")
            for f in missing[:10]:
                say(f"  {f}")
            if len(missing) > 10:
                say(f"  ...and {len(missing) - 10} more")
            say(f"(Checked from {Path.cwd()} and from the CSV's folder.)")
            if not ask_yes_no("Continue anyway?"):
                return 1
    elif sheet.identifier_column != "identifier":
        sys.exit("`ia metadata --spreadsheet` needs the column to be named `identifier` (not `item`).")

    ia = find_ia()
    cmd = build_command(ia, mode, csv_path, args.retries)
    log_path = csv_path.with_name(f"{csv_path.stem}_log.txt")

    say("")
    say(f"CSV:      {csv_path}")
    say(f"Items:    {sheet.item_count()}  (rows: {len(sheet.rows)})")
    say(f"Mode:     {MODE_LABELS[mode]}")
    say(f"Log:      {log_path}")
    if run_dir != Path.cwd():
        say(f"Run from: {run_dir}  (the file paths in the CSV are relative to it)")
    say(f"Command:  {' '.join(cmd)}")
    say(f"Retries:  up to {args.max_attempts} runs, {args.wait}s apart\n")
    if not args.yes and not ask_yes_no("Start?", default=True):
        return 1

    previous_failed: set[str] | None = None
    with open(log_path, "ab") as log, KeepAwake():
        def note(msg: str) -> None:
            say(msg)
            log.write(f"{msg}\n".encode("utf-8"))
            log.flush()

        note(f"\n===== {stamp()}  IA Retry Wrapper {__version__}: {MODE_LABELS[mode]} =====")
        note(f"Command: {' '.join(cmd)}")

        for attempt in range(1, args.max_attempts + 1):
            note(f"\n----- Attempt {attempt} of {args.max_attempts}  ({stamp()}) -----")
            try:
                code, crashed, failed = run_once(cmd, log, run_dir)
            except KeyboardInterrupt:
                note(f"\nStopped by you at {stamp()}.")
                return 130

            if crashed:
                if attempt == args.max_attempts:
                    break
                note(f"\nCrashed (attempt {attempt} of {args.max_attempts}). "
                     f"Retrying in {args.wait}s... (Ctrl+C to stop)")
                try:
                    time.sleep(args.wait)
                except KeyboardInterrupt:
                    note(f"\nStopped by you at {stamp()}.")
                    return 130
                continue

            if failed:
                if failed == previous_failed or attempt == args.max_attempts:
                    note(f"\nFinished, but these file(s) failed on every pass ({stamp()}):")
                    for f in sorted(failed):
                        note(f"  {f}")
                    note("Check them in the log; the problem is probably the file or its row, "
                         "not the connection.")
                    return 1
                note(f"\n{len(failed)} file(s) reported errors. Running another pass to retry them...")
                previous_failed = failed
                continue

            note(f"\nFinished with no crash. ({stamp()})")
            if code != 0 and mode != "metadata-update":
                note("(ia's exit code was non-zero; that's expected when --checksum skips "
                     "files that were already uploaded.)")
            elif code != 0:
                note("ia metadata reported a problem with at least one item; check the log above.")
                return 1
            return 0

        note(f"\nGave up after {args.max_attempts} attempts ({stamp()}). "
             f"Run it again to pick up where it left off.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
