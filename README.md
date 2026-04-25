# AI Usage Reports

A Slack bot that generates and distributes AI usage reports for team leaders, powered by [Hex](https://hex.tech) and Google Drive.

## How It Works

1. **Report generation** — Screenshots of each team leader's Hex dashboard are captured using Playwright and Chrome, then uploaded to Google Drive.
2. **Slack bot** — A persistent Slack bot listens for commands 24/7. Team leaders can request their report at any time by typing in Slack.
3. **Cloud VM** — The Slack bot runs on a Google Cloud VM (e2-micro, free tier), so it's always available without needing your laptop to be on.

## Architecture

```
Laptop (report generation)
  └── generate_all_reports.py
        ├── Chrome (Hex login session)
        ├── Playwright (screenshots)
        └── Google Drive (upload)

Google Cloud VM (always on)
  └── slack_bot.py
        ├── Listens for Slack commands
        └── Serves reports from Google Drive cache
```

## Slack Commands

| Command | Description |
|---|---|
| `report` | Sends you your own cached report as a DM |
| `get report <name>` | Sends you a specific team leader's report (managers only) |
| `generate reports` | Generates fresh reports for all team leaders (local bot only) |
| `generate reports <name>` | Generates a fresh report for one team leader (local bot only) |

## Project Structure

| File | Description |
|---|---|
| `slack_bot.py` | Slack bot — runs on the VM, serves reports on demand |
| `generate_all_reports.py` | Generates screenshots for all team leaders and uploads to Google Drive |
| `hex_screenshot.py` | Playwright-based screenshot engine for Hex dashboards |
| `send_report.py` | Triggers a Hex run and emails the report link to a team leader |
| `scheduler.py` | Weekly scheduler — sends reports to all team leaders via email |
| `requirements.txt` | Python dependencies |

## Setup

### Prerequisites

- Python 3.10+
- Google Chrome installed
- A Slack app with Socket Mode enabled
- A Hex account with an API token
- A Google Cloud service account with Drive access

### Installation

```bash
git clone https://github.com/eboom79/ai-usage-reports.git
cd ai-usage-reports
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
```

### Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Slack app token (`xapp-...`) |
| `SLACK_CHANNEL_ID` | Channel ID where the bot listens |
| `HEX_API_TOKEN` | Hex API token |
| `HEX_PROJECT_ID` | Hex project UUID |
| `GOOGLE_DRIVE_CREDENTIALS` | Path to Google service account JSON |
| `GOOGLE_DRIVE_FOLDER_ID` | Google Drive folder ID for report storage |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | Email config for sending reports |
| `SERVE_ONLY` | Set to `true` on the VM to disable report generation |

### Running the Slack Bot

```bash
python3 slack_bot.py
```

To run the bot with a different env file, pass `--env-file`:

```bash
python3 slack_bot.py --env-file .env0
```

### Generating Reports

```bash
python3 generate_all_reports.py
```

Make sure Chrome is running with remote debugging enabled first:

```bash
google-chrome --remote-debugging-port=9222
```

### Deploying to Google Cloud VM

1. Create an **e2-micro** VM (free tier eligible) in `us-central1`, `us-east1`, or `us-west1`
2. Upload all project files to the VM
3. Install dependencies (same as above)
4. Set `SERVE_ONLY=true` in `.env`
5. Use `supervisor` to keep the bot running 24/7

### Deploying on env0 / AWS

For env0, the simplest setup is to run the Slack bot as a long-lived process on an AWS VM and inject secrets as environment variables.

Recommended env0 variables:

| Variable | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | Slack bot token |
| `SLACK_APP_TOKEN` | Slack app-level Socket Mode token |
| `SLACK_CHANNEL_ID` | Optional channel restriction |
| `SERVE_ONLY=true` | Disable local report generation on the server |
| `GOOGLE_DRIVE_FOLDER_ID` | Google Drive folder holding generated reports |
| `GOOGLE_DRIVE_CREDENTIALS_JSON` | Full Google service-account JSON as a secret env var |
| `REPORTS_DIR=.hex_reports` | Local cache directory on the instance |
| `TEAM_LEADERS_JSON` | Full team-leader JSON as a single-line env var, if `team_leaders.json` is not deployed |
| `ENV_FILE=.env` | Optional if you still want a dotenv file loaded |

Startup command:

```bash
./run_env0_bot.sh
```

Notes:

1. `run_env0_bot.sh` creates a local virtualenv in `.venv`, installs `requirements.txt`, and starts the Slack bot.
2. The bot now supports `GOOGLE_DRIVE_CREDENTIALS_JSON`, which is usually easier to manage in env0 than shipping a JSON file path to the instance.
3. `team_leaders.json` is ignored by git, so env0 should either provide that file on the VM or set `TEAM_LEADERS_JSON` to the full JSON contents.
4. If env0 injects variables directly into the process, the `.env` file can be minimal or even absent.
5. This bot is designed for a single long-running instance because it keeps a PID file at `/tmp/slack_bot.pid`.
