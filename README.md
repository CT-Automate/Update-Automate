# NexisMonitor

NexisMonitor captures one temporary Nexus panel screenshot, analyzes it immediately, deletes the screenshot, and can send Slack notifications to two channels. JSON persistence is optional and is disabled in GitHub Actions by default.

## Structure

- `collector/analyze_panel.py`: screenshot parser, JSON writer, and Slack notifier.
- `collector/capture_only.py`: screenshot capture script for GitHub Actions or local runs.
- `collector/requirements.txt`: Python dependencies for capture and analysis.
- `collector/output/`: optional generated JSON output for local debugging only.
- Screenshots are stored only in the temp directory during a run and are deleted after analysis.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r collector\requirements.txt
Copy-Item collector\config.example.json collector\config.local.json
```

The analyzer does not require Tesseract or RapidOCR. It uses the fixed dashboard layout and local Windows font templates to read the large numeric values from each configured card.

## Configure

Edit `collector/config.local.json`:

- `layout.nodes`: the node cards to read. The default five departments are `MEI`, `Fitting`, `QC`, `Packing`, and `Manifest` from the middle row.
- `green_max` and `amber_max`: per-node color bands. Green is `value <= green_max`, amber is `green_max < value <= amber_max`, and red/breach is `value > amber_max`.
- Slack config can come from repository secrets instead of `collector/config.local.json`. Use either `SLACK_BOT_TOKEN` + `SLACK_ALL_UPDATES_CHANNEL` + `SLACK_BREACH_UPDATES_CHANNEL`, or the two webhook secrets `SLACK_ALL_UPDATES_WEBHOOK` and `SLACK_BREACH_UPDATES_WEBHOOK`. Set `SLACK_ENABLED=true` to turn notifications on. Set `SLACK_BREACH_UPDATES_INTERVAL_HOURS=10` to control how often the breach destination is notified while amber/red issues remain.
- For GitHub Actions capture, set repository secrets `NEXIS_EMPLOYEE_CODE`, `NEXIS_PASSWORD`, `NEXIS_URL`, `NEXIS_FACILITY`, and optionally `NEXIS_USER_DATA_DIR`, `NEXIS_BROWSER_CHANNEL`. The workflow sets `NEXIS_WRITE_JSON=false`, so extracted values are not uploaded or persisted as artifacts.

Default department bands:

| Department | Green | Amber | Red |
| --- | --- | --- | --- |
| MEI | `<=2500` | `<=3500` | `>3500` |
| Fitting | `<=2000` | `<=2500` | `>2500` |
| QC | `<=1500` | `<=2000` | `>2000` |
| Packing | `<=2500` | `<=4000` | `>4000` |
| Manifest | `<=2000` | `<=2500` | `>2500` |

## Run

Scan every discovered screenshot from the NexisMonitor `data` folder. This works from any terminal directory:

```powershell
.\.venv\Scripts\python.exe collector\analyze_panel.py
```

Process only the newest discovered screenshot:

```powershell
.\.venv\Scripts\python.exe collector\analyze_panel.py --latest
```

Test one screenshot directly without sending Slack:

```powershell
.\.venv\Scripts\python.exe collector\analyze_panel.py --image C:\path\to\screenshot.png --date 2026-07-09 --time 1806 --no-slack
```

Local JSON output is optional. Set `NEXIS_WRITE_JSON=true` only when you intentionally want debug output under:

```text
collector/output/<year>/<yyyy-mm-dd>/<hhmm>.json
```

Slack behavior:

- The all-updates destination receives every processed update on every run.
- The breach destination receives a message only when at least one department is amber or red, includes only amber/red departments, and sends only on the configured interval, default `SLACK_BREACH_UPDATES_INTERVAL_HOURS=10`.
- If any configured node cannot be read, the status is `READ_ERROR`, not `OK`, and the JSON includes `has_read_error` plus `read_failed_nodes`.
