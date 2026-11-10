# IA Retry Wrapper

Keeps an Internet Archive spreadsheet upload running until it finishes, so you
don't have to babysit it.

`ia upload --spreadsheet=...` stops the whole batch if a single request times
out or the connection drops. IA Retry Wrapper restarts it automatically, and
`--checksum` makes each restart skip files that already made it up. It works on
macOS, Windows and Linux.

## What it does

- **Asks for the CSV.** You can type the path, paste it, or drag the file into
  the terminal window.
- **Asks what kind of upload it is:**
  1. **Metadata only.** Adds `--no-derive`.
  2. **Includes assets or media files.** Derive runs as normal.
- **Notices a missing `file` column** and asks whether the sheet is:
  1. **A metadata update.** It runs `ia metadata --spreadsheet` to change
     metadata on items that are already on the Archive.
  2. **Incomplete.** It stops, so you can fix the sheet.
- **Checks the sheet before starting.** It warns about empty `file` cells and
  files it can't find. If the file paths are relative to the CSV's folder, it
  runs from that folder automatically.
- **Retries crashes.** If `ia` crashes (timeouts, dropped Wi-Fi), it waits and
  runs again, up to 20 times by default.
- **Retries failed files once.** If individual files fail, it runs one more
  pass. If the same files fail again, it stops and lists them, since the
  problem is probably the file or its row rather than the connection.
- **Writes a log next to the CSV,** named after it. For example,
  `my_batch.csv` gets `my_batch_log.txt`. Each run appends to the log with
  timestamps.
- **Keeps the computer awake** while it runs.
- **Prints `Finished with no crash.`** when the batch is done.

## Install

You need Python 3.8 or newer, and the `ia` tool set up with your Archive
account.

```bash
pip install internetarchive
ia configure
```

Then install the wrapper straight from GitHub. Replace `YOUR-USERNAME` with the
GitHub account that hosts the repo.

```bash
pip install git+https://github.com/YOUR-USERNAME/ia-retry-wrapper.git
```

If `pip` complains about an "externally managed environment" (common with
Homebrew Python), use [pipx](https://pipx.pypa.io/) instead:

```bash
pipx install git+https://github.com/YOUR-USERNAME/ia-retry-wrapper.git
```

You can also skip installing: download `ia_retry_wrapper.py` and run it with
`python3 ia_retry_wrapper.py` (on Windows, `py ia_retry_wrapper.py`).

## Use

```bash
ia-retry
```

Answer the prompts, check the summary, and press Enter to start.

To skip the prompts:

```bash
ia-retry path/to/batch.csv --mode assets -y
```

| Option | What it does |
|---|---|
| `--mode metadata-only` | Upload with `--no-derive` |
| `--mode assets` | Upload with derive |
| `--mode metadata-update` | `ia metadata --spreadsheet`, for sheets with no `file` column |
| `--max-attempts N` | Give up after N runs (default 20) |
| `--wait SECONDS` | Pause before retrying after a crash (default 120) |
| `--retries N` | Passed to `ia upload --retries` (default 10) |
| `-y`, `--yes` | Start without the final confirmation |

Press Ctrl+C at any time to stop. Running it again picks up where it left off.

## Why it doesn't trust ia's exit code

In spreadsheet mode, `ia upload --checksum` counts files it *skipped* as
errors. That means a batch that has fully finished still exits with a failure
code, and a simple `until ia upload ...; do ...; done` loop never stops. IA
Retry Wrapper reads the output instead:

- A Python `Traceback` means `ia` crashed, so the run is retried.
- `error uploading` lines mean specific files failed, so they get another pass.
- Anything else means the batch is done.

## CSV format

This is the same spreadsheet format `ia upload --spreadsheet` uses:

- An `identifier` column (or `item`).
- A `file` column listing the file to upload.
- Any metadata columns you want, such as `title`, `date`, `collection`, or
  `subject[0]`.

Several rows can belong to one item: leave `identifier` blank on the rows after
the first, and they'll be added to the item above.

For metadata updates, leave out the `file` column and name the identifier
column `identifier`.

## License

MIT
