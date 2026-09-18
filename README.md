# Himalayas Hiring Assistant

A local-first recruiting assistant with a Chromium extension UI and a Python backend.

## What it does

- Imports candidate profiles from the Himalayas MCP server.
- Classifies profiles as developer or non-developer using the candidate's visible stack and expertise.
- Generates concise first outreach and replies through OpenRouter.
- Grounds every DeepSeek recruiting message in the Oceanparkasset company brief and website context, including its AI crypto-trading platform, execution workflow, and risk controls.
- Schedules first outreach when Start page outreach is clicked, then sends one message at a time using the configured interval and reports queue, sent, skipped, and failed states.
- Supports automatic campaigns: sync page 1, queue and finish its outreach, then continue through later pages until MCP returns no more candidates.
- Uses bounded parallel MCP profile enrichment and AI message generation so sync and queue creation do not wait on every candidate serially.
- Schedules follow-up messages one at a time per candidate with a content-aware delay.
- Records inbound replies and generates the next recruiting message.
- Polls the configured MCP message tool, detects candidate replies, and automatically queues the next hiring-logic response without duplicating already processed messages.
- Guides engaged chats through profile-specific questions, a satisfaction and technical-assessment step, then requests a GitHub username for project access after the assessment stage.
- Detects GitHub usernames or emails in replies and sends a repository invitation when GitHub token, owner, and repository settings are configured.
- Processes Himalayas talent pages in order. Sync page 1, start that page's outreach, then use Next page to import and queue page 2, page 3, and later pages.
- Checks the Supabase contact ledger before every first message and skips any member whose `talent_slug` is already saved.
- Keeps campaign state in SQLite so the extension can be closed without losing work.

The first-outreach action creates a paced delivery queue when started. The scheduler sends one due campaign message at a time using `MIN_MESSAGE_DELAY_SECONDS`; follow-up messages remain controlled by `AUTO_SEND`. Use it only where the platform, candidate consent, and applicable law permit automated outreach.

## Oceanparkasset recruiting context

DeepSeek receives a dedicated Oceanparkasset recruiting brief for every first message and reply. Messages must connect verified candidate experience to relevant platform work, follow ASD STE100, and avoid investment solicitation, profit claims, unsupported company claims, or invented role details.

## Setup

1. Rotate the OpenRouter key that was pasted into chat, then copy `.env.example` to `.env` and provide the replacement key.
2. Set `HIMALAYAS_MCP_URL` to the official Himalayas MCP endpoint and add `HIMALAYAS_MCP_TOKEN` if that server requires authentication. The MCP tool names are configurable because deployments can expose different names.
3. Install the backend as a background service that starts automatically on login, so you never run `uvicorn` by hand. Pick the script for your OS (see [Running on another machine](#running-on-another-machine) below for Windows/macOS):

   ```bash
   ./scripts/install-service.sh   # Linux (systemd)
   ```

   This creates a virtual environment, installs dependencies, and registers a `systemd --user` service (`him-hiring-assistant.service`) that starts on login and restarts itself if it ever crashes. To remove it later, run `./scripts/uninstall-service.sh`. Useful commands:

   ```bash
   systemctl --user status him-hiring-assistant.service   # check it's running
   journalctl --user -u him-hiring-assistant.service -f   # follow logs
   systemctl --user restart him-hiring-assistant.service  # restart after a code or .env change
   ```

   (If you'd rather run it manually for development, `uvicorn app.main:app --reload --port 8765` still works — but avoid `--reload` for anything long-running: an open extension connection can make it hang mid-reload.)

Before first use, run `supabase_schema.sql` in the Supabase SQL Editor. The send path fails closed until Supabase is reachable and the table exists.

4. Load `extension/` in Chrome or Chromium at `chrome://extensions` using **Load unpacked** — select the `extension` folder itself, not the project root. As long as the background service is running (step 3), the extension works immediately — no separate server process to start each time.

## Running on another machine

Each machine needs its own copy of the backend running as a background service — the extension alone has no server logic. On a new machine:

1. Copy the project folder over (everything except `.venv`, `__pycache__`, and `hiring.db` — leave those out and let the install script and app recreate them).
2. Create `.env` from `.env.example` and fill in the same credentials (OpenRouter key, `HIMALAYAS_MCP_URL`, Supabase URL/key, GitHub token if used). Reuse the **same Supabase project** so the "already contacted" ledger stays shared and accurate across machines.
3. Run the install script for that OS:

   | OS | Script |
   |---|---|
   | Linux | `./scripts/install-service.sh` |
   | macOS | `./scripts/install-service-macos.sh` |
   | Windows | `powershell -ExecutionPolicy Bypass -File .\scripts\install-service-windows.ps1` |

   Each script creates a virtual environment, installs dependencies, and registers the backend to start automatically at login (`systemd --user` on Linux, `launchd` on macOS, Task Scheduler on Windows).
4. Load `extension/` in Chrome on that machine via **Load unpacked**.
5. Open the extension and click **Connect Himalayas** to authorize that machine's backend — the OAuth token is stored locally per machine, so this step is needed again even if you copied `.env` over.

Only run the backend on **one machine at a time** against the same Himalayas account. Two live instances polling and sending concurrently can race on the same candidates/messages — this is the exact kind of duplicate-delivery bug already found and fixed earlier in this project (an orphaned duplicate process double-processing the same queue). Stop the service on one machine before starting it on another.

## API shape

- `GET /api/health`
- `GET /api/candidates`
- `POST /api/candidates/sync` imports candidates through MCP
- `POST /api/candidates/sync?page=2` imports a specific Himalayas talent page
- `POST /api/campaigns` creates a paced first-message queue for the selected page or candidates
- `POST /api/automation/start` starts the automatic page-by-page campaign runner
- `POST /api/automation/stop` stops the automatic campaign runner
- `GET /api/messages?status=queued` lists outbound messages
- `POST /api/messages/{id}/approve` approves a queued message
- `POST /api/messages/{id}/send` sends immediately through MCP
- `POST /api/candidates/{id}/replies` stores a reply and queues an AI response

The scheduler processes one due campaign or approved follow-up message at a time. The automatic runner waits for the current page's scheduled/approved messages to finish before moving to the next page. Because candidate profiles and messages are handled through MCP, the extension does not visit profile pages or navigate back through the talent list, so website scroll position is not changed by this workflow. Keep `AUTO_SEND=false` while validating your MCP tool names and message copy.

## ASD STE100

Generated prompts require short, direct, plain-English messages. They avoid idioms, unnecessary adjectives, unexplained abbreviations, and multiple questions in one message. The prompt is a guardrail, not a substitute for reviewing messages before use.

## MCP adapter

The adapter uses the official Himalayas MCP endpoint at `https://mcp.himalayas.app/mcp`. Candidate search uses `search_talent`; first outreach uses `start_conversation`; replies use `send_message`. These calls happen directly through MCP. The extension does not open a candidate profile, click a message button, scroll, or navigate back to a list page.

Himalayas employer messaging requires OAuth 2.1 with PKCE. Configure these tool names in `.env`:

- `MCP_LIST_CANDIDATES_TOOL`
- `MCP_SEND_MESSAGE_TOOL`
- `MCP_GET_PROFILE_TOOL`
- `MCP_LIST_MESSAGES_TOOL`

Performance settings:

- `PROFILE_FETCH_CONCURRENCY` controls parallel profile requests and defaults to `4`.
- `MESSAGE_GENERATION_CONCURRENCY` controls parallel AI drafts and defaults to `3`.
- `REPLY_PROCESSING_CONCURRENCY` controls how many independent candidate replies can be processed at once and defaults to `4`.
- `REPLY_POLL_INTERVAL_SECONDS` controls inbound reply polling and defaults to `30`.
- `DELIVERY_POLL_INTERVAL_SECONDS` controls how often due messages are checked and defaults to `5`.

The payload mapping is isolated in `app/mcp_client.py` so it can be adjusted to the exact official Himalayas MCP schema without changing scheduling or recruiting logic.