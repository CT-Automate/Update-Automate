# Update-Automate

Update-Automate is set up to run in GitHub Actions on an hourly schedule. It captures one temporary Nexus panel screenshot, analyzes the configured departments, sends Slack notifications, and deletes the screenshot after analysis.

## What the workflow does

Each run performs this sequence:

1. Logs into Nexs using secrets from GitHub Actions.
2. Opens the Monitor Panel for the configured facility.
3. Captures one temporary screenshot only.
4. Extracts the five configured departments: `MEI`, `Fitting`, `QC`, `Packing`, and `Manifest`.
5. Classifies each value using the threshold rules in `collector/config.example.json`.
6. Sends Slack updates based on the configured channels and interval.
7. Deletes the temporary screenshot.
8. Does not upload artifact output or persist extracted JSON in GitHub Actions by default.

## Where to change things

### GitHub repository secrets
Set these in `Settings` > `Secrets and variables` > `Actions`.

Nexus access:

- `NEXIS_EMPLOYEE_CODE`
- `NEXIS_PASSWORD`
- `NEXIS_URL`
- `NEXIS_FACILITY`
- `NEXIS_USER_DATA_DIR` if you need a custom browser profile path
- `NEXIS_BROWSER_CHANNEL` if you need a specific browser channel

Slack routing:

- `SLACK_ENABLED`
- `SLACK_BOT_TOKEN`
- `SLACK_ALL_UPDATES_CHANNEL`
- `SLACK_BREACH_UPDATES_CHANNEL`
- `SLACK_ALL_UPDATES_WEBHOOK`
- `SLACK_BREACH_UPDATES_WEBHOOK`
- `SLACK_BREACH_UPDATES_INTERVAL_HOURS`

Recommended Slack setup:

- Use `SLACK_ENABLED=true` to turn notifications on.
- Use either bot token mode or webhook mode, not both.
- Set `SLACK_BREACH_UPDATES_INTERVAL_HOURS=10` if you want the breach channel to send on a 10-hour interval while amber/red conditions remain.

### Workflow file
Edit [.github/workflows/nexis.yml](./.github/workflows/nexis.yml) if you need to change:

- the schedule
- the browser version
- the environment variable mapping
- the capture command

The current schedule is hourly:

```yaml
schedule:
  - cron: '0 * * * *'
```

### Analysis rules
Edit [collector/config.example.json](./collector/config.example.json) if you need to change:

- the departments being read
- the `value_box` coordinates
- the green and amber thresholds
- the Slack status symbols

Default department bands:

| Department | Green | Amber | Red |
| --- | --- | --- | --- |
| MEI | `<=2500` | `<=3500` | `>3500` |
| Fitting | `<=2000` | `<=2500` | `>2500` |
| QC | `<=1500` | `<=2000` | `>2000` |
| Packing | `<=2500` | `<=4000` | `>4000` |
| Manifest | `<=2000` | `<=2500` | `>2500` |

## Slack behavior

- The all-updates channel receives every processed update on every run.
- The breach channel receives only amber/red departments.
- The breach channel sends only on the configured interval, default `SLACK_BREACH_UPDATES_INTERVAL_HOURS=10`.
- If any configured node cannot be read, the status becomes `READ_ERROR` and the payload records `has_read_error` plus `read_failed_nodes`.

## Files of interest

- [collector/capture_only.py](./collector/capture_only.py): captures the temporary screenshot and triggers analysis.
- [collector/analyze_panel.py](./collector/analyze_panel.py): extracts values, builds payloads, and sends Slack updates.
- [.github/workflows/nexis.yml](./.github/workflows/nexis.yml): GitHub Actions workflow.
- [collector/config.example.json](./collector/config.example.json): threshold and layout defaults.

