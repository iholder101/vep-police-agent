# VEP Police Agent

An AI-powered governance agent for monitoring and managing KubeVirt [Virtualization Enhancement Proposals](https://github.com/kubevirt/enhancements)
(VEPs).
This agent continuously tracks VEP progress, monitors compliance, checks deadlines,
and maintains a Google Sheets dashboard with real-time VEP status.

It is largely inspired by @vladikr's work on https://github.com/vladikr/vepMonitoring.

## Purpose

The VEP Police Agent automates the monitoring and governance of the KubeVirt VEP process by:

- **Discovering and tracking all VEPs** from GitHub issues and documentation
- **Monitoring compliance** with VEP process requirements (SIG sign-offs, template completeness, etc.)
- **Tracking deadlines** (Enhancement Freeze, Code Freeze) and sending alerts
- **Monitoring activity** on VEPs and flagging inactive ones
- **Maintaining a Google Sheets dashboard** with comprehensive VEP status
- **Detecting exceptions** and tracking post-freeze work
- **Sending email alerts** for deadline warnings, compliance issues, low activity, and risk indicators

The agent uses Large Language Models (LLMs) via Google's Gemini API to intelligently analyze VEP data, GitHub issues, and PRs, making it capable of understanding context and making nuanced decisions about VEP status and compliance.

## Features

- 🤖 **AI-Powered Analysis**: Uses Gemini models to intelligently analyze VEP data and GitHub content
- 📊 **Google Sheets Integration**: Maintains a real-time dashboard with VEP status, compliance flags, and alerts
- 📧 **Email Alerts**: Sends structured email notifications for deadlines, compliance issues, low activity, and risks
- 💬 **Slack Alerts**: Sends color-coded Slack notifications via Incoming Webhooks (parallel with email)
- 🔍 **Comprehensive VEP Discovery**: Finds VEPs from GitHub issues, PRs, and documentation files
- 📋 **GitHub Project V2 Integration**: Fetches VEP status and metadata directly from kubevirt project boards
- 🔗 **Implementation PR Tracking**: Pre-computes VEP-to-PR mappings from kubevirt/kubevirt repository
- ⏰ **Deadline Monitoring**: Tracks Enhancement Freeze (EF) and Code Freeze (CF) dates from release schedules
- ✅ **Compliance Checking**: Verifies SIG sign-offs, template completeness, and process adherence
- 📈 **Activity Monitoring**: Flags inactive VEPs and tracks review lag times
- 🔄 **Continuous Operation**: Runs continuously or in one-cycle mode for scheduled jobs
- 🚀 **Containerized**: Runs in Podman/Docker containers for easy deployment
- 🔐 **MCP Integration**: Uses Model Context Protocol (MCP) for GitHub and Google Sheets access

## Architecture

The agent is built using **LangGraph** for orchestration and follows a node-based architecture:

### Node Graph

The agent's execution flow is orchestrated by a central scheduler that routes to different nodes:

```mermaid
graph TD
    Start([Start]) --> Scheduler[scheduler]

    Scheduler -->|Periodic| FetchVEPs[fetch_veps]
    Scheduler -->|Need analysis| FetchVEPs
    Scheduler -->|Sheets due| UpdateSheets[update_sheets]
    Scheduler -->|Alerts due| AlertSummary[alert_summary]
    Scheduler -->|No tasks| Wait[wait]

    FetchVEPs --> RunMonitoring[run_monitoring]

    RunMonitoring --> CheckDeadlines[check_deadlines]
    RunMonitoring --> CheckActivity[check_activity]
    RunMonitoring --> CheckCompliance[check_compliance]
    RunMonitoring --> CheckExceptions[check_exceptions]

    CheckDeadlines --> MergeUpdates[merge_vep_updates]
    CheckActivity --> MergeUpdates
    CheckCompliance --> MergeUpdates
    CheckExceptions --> MergeUpdates

    MergeUpdates --> AnalyzeCombined[analyze_combined]
    AnalyzeCombined --> SaveStateCache[save_state_cache]
    SaveStateCache --> Scheduler

    AlertSummary -->|Alerts| SendNotifications[send_notifications]
    AlertSummary -->|No alerts| Scheduler

    SendNotifications --> SendEmail[send_email]
    SendNotifications --> SendSlack[send_slack]

    UpdateSheets --> Scheduler
    SendEmail --> Scheduler
    SendSlack --> Scheduler
    Wait --> Scheduler

    Scheduler -.->|Loop| Scheduler

    style Scheduler fill:#2196F3,stroke:#1976D2,stroke-width:2px,color:#fff
    style RunMonitoring fill:#FF9800,stroke:#F57C00,stroke-width:2px,color:#fff
    style MergeUpdates fill:#4CAF50,stroke:#388E3C,stroke-width:2px,color:#fff
    style AnalyzeCombined fill:#9C27B0,stroke:#7B1FA2,stroke-width:2px,color:#fff
    style SaveStateCache fill:#673AB7,stroke:#512DA8,stroke-width:2px,color:#fff
    style UpdateSheets fill:#F44336,stroke:#D32F2F,stroke-width:2px,color:#fff
    style FetchVEPs fill:#00BCD4,stroke:#0097A7,stroke-width:2px,color:#fff
    style CheckDeadlines fill:#795548,stroke:#5D4037,stroke-width:2px,color:#fff
    style CheckActivity fill:#607D8B,stroke:#455A64,stroke-width:2px,color:#fff
    style CheckCompliance fill:#9E9E9E,stroke:#616161,stroke-width:2px,color:#fff
    style CheckExceptions fill:#FFC107,stroke:#F57C00,stroke-width:2px,color:#000
    style AlertSummary fill:#E91E63,stroke:#C2185B,stroke-width:2px,color:#fff
    style SendNotifications fill:#FF5722,stroke:#E64A19,stroke-width:2px,color:#fff
    style SendEmail fill:#FF5722,stroke:#E64A19,stroke-width:2px,color:#fff
    style SendSlack fill:#4A154B,stroke:#3C1042,stroke-width:2px,color:#fff
    style Wait fill:#9E9E9E,stroke:#616161,stroke-width:2px,color:#fff
```

**Flow Description:**
1. **Entry Point**: The `scheduler` node is the central coordinator and entry point
2. **VEP Discovery**: Routes to `fetch_veps` in two cases:
   - **Priority**: If no VEPs exist (immediate fetch on first run)
   - **Periodic**: Every configured interval (default: 1 hour) to refresh and discover new VEPs
3. **Automatic Analysis Pipeline**: After `fetch_veps` completes, the scheduler automatically schedules `run_monitoring` to ensure VEPs always go through the full analysis pipeline before updating sheets or sending emails
4. **Parallel Context Fetch**: `run_monitoring` triggers four parallel **fetch nodes** (using lightweight Flash model):
   - `check_deadlines`: Fetches deadline context (days to EF/CF, freeze status)
   - `check_activity`: Fetches activity context (last updates, recent events)
   - `check_compliance`: Fetches compliance context (PR status, labels, template)
   - `check_exceptions`: Fetches exception context (exception issues, post-freeze work)
5. **Deterministic Merge**: `merge_vep_updates` applies raw context data to VEPs (no LLM needed)
6. **Holistic Analysis**: `analyze_combined` (using powerful Pro model) performs ALL reasoning:
   - Cross-domain analysis (e.g., low activity + close deadline = URGENT)
   - Generates alerts for issues needing attention
   - Updates VEP priority and recommended actions
7. **State Cache**: After `analyze_combined`, `save_state_cache` saves state to `cache/state_cache.json` for fast debug cycles with `--use-state-cache`
8. **Post-Analysis Actions**: After caching, routes back to `scheduler`, which automatically schedules both `update_sheets` and `alert_summary` in parallel:
   - `update_sheets`: Updates Google Sheets with VEP status
   - `alert_summary`: Formats alerts for notifications (limits to ~20 most significant)
9. **Notifications**: `alert_summary` conditionally routes to `send_notifications` (if alerts exist) or back to `scheduler` (if no alerts). `send_notifications` fans out to `send_email` and `send_slack` in parallel
10. **Wait Loop**: If no tasks, waits until next round hour (or next interval if `--immediate-start` is used) before returning to scheduler (continuous operation)

**Key Design Principles:**
- **Separation of Concerns**: Fetch nodes gather raw data (Flash), merge node combines context (deterministic), analyze node does reasoning (Pro)
- **Single Analysis Point**: All cross-domain reasoning happens in `analyze_combined`, ensuring holistic insights
- **Parallel Execution**: `update_sheets` and `alert_summary` run in parallel after analysis completes
- **Scheduler-Driven**: The scheduler is the central coordinator that ensures proper sequencing

## Requirements

- Python 3.11+
- Podman or Docker (for containerized execution)
- Google Gemini API key
- Google Service Account credentials (for Google Sheets access)
- GitHub Personal Access Token (optional, but recommended for higher rate limits)
- Resend API key (optional, required for email alerts)
- Slack Incoming Webhook URL (optional, required for Slack alerts)

## Installation

### 1. Clone the Repository

```bash
git clone <repository-url>
cd vep-police-agent
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Set Up Credentials

Create the following files in the project root:

**API_KEY**: Your Google Gemini API key
```bash
echo "your-gemini-api-key" > API_KEY
```

**GOOGLE_TOKEN**: Google Service Account JSON credentials for Google Sheets access
```bash
# Copy your service account JSON file content to GOOGLE_TOKEN
cat path/to/service-account.json > GOOGLE_TOKEN
```

**GITHUB_TOKEN** (optional but recommended): GitHub Personal Access Token
```bash
echo "your-github-token" > GITHUB_TOKEN
```

**RESEND_API_KEY** (optional, required for email alerts): Resend API key for sending emails
```bash
# Get free API key from https://resend.com/api-keys
echo "re_..." > RESEND_API_KEY
```

**SLACK_WEBHOOK_URL** (optional, required for Slack alerts): Slack Incoming Webhook URL
```bash
# Get webhook URL from your Slack app configuration
echo "https://hooks.slack.com/services/..." > SLACK_WEBHOOK_URL
```

### 4. Build Container Image (Optional)

If you want to run in a container:

```bash
cd container
./build-and-push.sh
```

## Usage

### Running Locally

```bash
python main.py \
    --api-key API_KEY \
    --google-token GOOGLE_TOKEN \
    --github-token GITHUB_TOKEN \
    --sheet-id YOUR_SHEET_ID
```

### Running in Container

The easiest way is to use the provided scripts:

```bash
# Run one cycle and exit (useful for cron jobs)
./scripts/run-one-cycle.sh

# Run continuously
./scripts/run-latest-agent.sh

# Create systemd unit for running as a service
sudo ./scripts/create-systemd-unit.sh

# After creating the unit:
sudo systemctl start vep-police-agent
sudo systemctl enable vep-police-agent  # Auto-start on boot
journalctl -u vep-police-agent -f      # View logs

# Delete the systemd unit
sudo ./scripts/create-systemd-unit.sh --delete
```

**Raw Container Run** (using podman/docker directly):

```bash
# From the project root directory
podman run --rm --pull=newer \
    -v "$(pwd):/workspace:ro" \
    -v "$(pwd)/cache:/workspace/cache:rw" \
    -w /workspace \
    quay.io/mabekitzur/vep-police-agent:latest \
    --api-key /workspace/API_KEY \
    --google-token /workspace/GOOGLE_TOKEN \
    --github-token /workspace/GITHUB_TOKEN \
    --sheet-id YOUR_SHEET_ID \
    --one-cycle
```

**Note**:
- `-v "$(pwd):/workspace:ro"` mounts your project directory as read-only (credentials stay secure)
- `-v "$(pwd)/cache:/workspace/cache:rw"` mounts cache directory as read-write for persistence
- `-w /workspace` sets the working directory so `/workspace/API_KEY` paths work correctly
- Run this from the project root directory where your credential files are located

### Command Line Options

- `--api-key PATH`: Path to Google Gemini API key file
- `--google-token PATH`: Path to Google Service Account JSON file
- `--github-token PATH`: Path to GitHub Personal Access Token file (optional but recommended)
- `--resend-api-key PATH`: Path to Resend API key file for email sending (get free key at https://resend.com/api-keys)
- `--slack-webhook-url PATH`: Path to Slack Incoming Webhook URL file (default: `SLACK_WEBHOOK_URL`)
- `--sheet-id ID`: Google Sheets document ID (from URL: `https://docs.google.com/spreadsheets/d/{ID}/edit`)
- `--one-cycle`: Run one cycle and exit after sheet update completes
- `--immediate-start`: Run the first cycle immediately without waiting for round hour. Subsequent cycles will use current time + interval instead of round hours (useful for testing and one-time runs)
- `--skip-monitoring`: Skip all monitoring checks (deadlines, activity, compliance, exceptions) for faster debugging
- `--skip-sheets`: Skip Google Sheets updates (useful for testing email alerts)
- `--skip-send-email`: Skip sending email alerts (useful for debugging without sending emails)
- `--skip-send-slack`: Skip sending Slack alerts (useful for debugging without sending Slack messages)
- `--fastest-model`: Force all nodes to use `GEMINI_3_FLASH_PREVIEW` (fastest model)
- `--mock-veps`: Use mock VEPs instead of fetching from GitHub (useful for testing without API calls)
- `--mock-analyzed-combined`: Skip LLM call in analyze_combined node and use naive analysis (faster testing)
- `--mock-alert-summary`: Skip LLM call in alert_summary node and create mocked alerts (faster testing)
- `--use-state-cache`: Use cached state from previous run on first cycle (skips fetch/analyze). Cache is created after each full analysis run. Useful for fast debug/test cycles.
- `--debug MODE`: Enable debug mode (`discover-veps` or `test-sheets`)
- `--index-cache-minutes MINUTES`: Maximum age of index cache in minutes (default: 60)
- `--no-index-cache`: Disable index caching

### Debug Modes

**Discover VEPs** (indexes and prints VEP data, then exits):
```bash
./scripts/debug/debug-vep-index.sh
```

**Test Sheets** (tests Google Sheets integration with mock data):
```bash
./scripts/debug/debug-test-sheets.sh
```

**Test Email** (tests email alert functionality with mock data):
```bash
./scripts/debug/debug-test-email.sh
```

**Test Slack** (tests Slack alert functionality with mock data):
```bash
./scripts/debug/debug-test-slack.sh
```

## Configuration

### Model Selection

Models are configured per node in `config.py`. The architecture uses a two-tier approach:

- **Context Fetch nodes** (Flash - fast and cheap):
  - `fetch_veps`, `check_deadlines`, `check_activity`, `check_compliance`, `check_exceptions`
  - These gather raw data only, no analysis

- **Analysis nodes** (Pro - powerful reasoning):
  - `analyze_combined`: Cross-domain reasoning, alert generation
  - `update_sheets`, `alert_summary`: Output formatting

- **Deterministic nodes** (no LLM):
  - `merge_vep_updates`: Applies context data to VEPs (pure Python)

Use `--fastest-model` to override all nodes to use Flash for testing.

### Google Sheets Setup

1. Create a Google Sheet (or use an existing one)
2. Share it with your service account email (found in `GOOGLE_TOKEN`)
3. Grant **Editor** access
4. Copy the Sheet ID from the URL: `https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit`
5. Pass the Sheet ID with `--sheet-id` or set `SHEET_ID` environment variable

### Email Alerts Configuration

Email alerts are configured in `config.py`:

```python
EMAIL_RECIPIENTS = [
    "iholder@redhat.com",
    "user2@example.com",
]
```

Or set `EMAIL_RECIPIENTS` environment variable (comma-separated):
```bash
export EMAIL_RECIPIENTS='user1@example.com,user2@example.com'
```

**Email Setup** (required for sending email alerts):

The agent uses [Resend](https://resend.com) for email delivery (easiest setup):
1. Sign up for a free account at https://resend.com
2. Get your API key from https://resend.com/api-keys
3. Create `RESEND_API_KEY` file with your API key: `echo "re_..." > RESEND_API_KEY`
4. Free tier: 3,000 emails/month, no domain verification needed for basic sending

**Alert Types**:
- `deadline_approaching`: Deadlines (EF, CF) approaching or passed
- `low_activity`: VEP has low/no activity (inactive, stale)
- `compliance_issue`: VEP has compliance problems (missing sign-offs, incomplete template, etc.)
- `risk`: VEP is at risk, requires exception, or has other risk indicators
- `status_change`: VEP status changed (new VEP, status update, etc.)
- `milestone_update`: VEP milestone status changed

### Slack Alerts Configuration

The agent can send alerts to Slack via Incoming Webhooks. Alerts are sent in parallel with email notifications.

**Slack Setup**:
1. Go to https://api.slack.com/apps and create a new app (or use existing)
2. Under "Features", click "Incoming Webhooks" and enable it
3. Click "Add New Webhook to Workspace" and select a channel
4. Copy the webhook URL
5. Create `SLACK_WEBHOOK_URL` file: `echo "https://hooks.slack.com/services/..." > SLACK_WEBHOOK_URL`

**Message Format**: Alerts are formatted using Slack Block Kit with color-coded attachments:
- Critical: Red (#d32f2f)
- High: Orange (#f57c00)
- Medium: Yellow (#fbc02d)
- Low: Green (#388e3c)

**Skip Slack**: Use `--skip-send-slack` to disable Slack notifications while keeping email enabled.

### Scheduling Configuration

The agent runs operations on configurable intervals. By default, all operations run on round hours (e.g., 13:00, 14:00, 15:00):

- **Fetch VEPs**: Every 1 hour (configurable via `FETCH_VEPS_INTERVAL_SECONDS` in `config.py`)
- **Update Sheets**: Every 1 hour (configurable via `UPDATE_SHEETS_INTERVAL_SECONDS` in `config.py`)
- **Alert Summary**: Every 1 hour (configurable via `ALERT_SUMMARY_INTERVAL_SECONDS` in `config.py`)

**Round-Hour Scheduling**: By default, operations wait until the next round hour (e.g., if it's 13:45, operations wait until 14:00). Use `--immediate-start` to run the first cycle immediately and use interval-based timing (current time + interval) instead of round hours.

### Caching

The agent caches data in the `cache/` directory to avoid redundant API calls:
- `cache/index_cache.json`: Indexed VEP data from GitHub
- `cache/state_cache.json`: State snapshot for `--use-state-cache`
- Default cache age: 60 minutes
- Use `--no-index-cache` to disable index caching
- Use `--index-cache-minutes` to adjust cache duration
- Cache directory is mounted read-write in container while workspace stays read-only

## Troubleshooting

### Google Sheets Access Denied

If you see "Requested entity was not found":
1. Ensure the sheet is shared with your service account email
2. Grant **Editor** access (not just Viewer)
3. Verify the Sheet ID is correct

### Rate Limit Errors

GitHub API rate limits:
- Without token: 60 requests/hour (IP-based)
- With token: 5000 requests/hour

The agent includes retry logic with exponential backoff for rate limit errors.

### Cache Issues

If VEP discovery seems stale:
- Use `--no-index-cache` to force fresh indexing
- Delete `cache/index_cache.json` manually
- Adjust `--index-cache-minutes` for your needs

## Credits

This project was developed with **heavy use of Cursor**, an AI-powered code editor. Cursor's advanced code generation, refactoring, and debugging capabilities were instrumental in implementing the complex agent logic, MCP integrations, and LangGraph orchestration. The iterative development process, prompt-driven code generation, and intelligent code completion provided by Cursor significantly accelerated the development of this agent.

The **GitHub Project V2 integration** and **implementation PR tracking** features were inspired by [vladikr/vepMonitoring](https://github.com/vladikr/vepMonitoring), which pioneered the use of GraphQL queries for project board data and VEP-to-PR mapping patterns.

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]
