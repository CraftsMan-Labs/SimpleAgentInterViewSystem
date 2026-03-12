# SimpleAgentInterViewSystem

This repository is a SaaS-style interview assistant that combines:

- `SimpleAgents` workflow execution for interview logic.
- `SimpleFlowSDK` integration for control-plane auth, onboarding, registration lifecycle, and invoke proxy APIs.
- A FastAPI backend and browser chat UI.

It includes:

- A one-question-at-a-time interview workflow.
- A local CLI chat runner using `simple-agents-py`.
- A FastAPI backend with a browser chat frontend.
- Control-plane proxy endpoints and machine-auth onboarding flow.
- Environment-based provider configuration.

## Structure

- `workflows/python-intern-fun-interview-chat.yaml` - chat workflow.
- `scripts/chat_agent.py` - interactive runner.
- `app.py` - FastAPI API and frontend host.
- `frontend/` - browser UI for interview chat.
- `.env.example` - local config template.

## Setup

```bash
cd /home/rishub/Desktop/projects/rishub/SimpleFlowTestTempaltes/SimpleAgentInterViewSystem
python -m venv .venv
source .venv/bin/activate
pip install -e /home/rishub/Desktop/projects/rishub/SimpleAgents/crates/simple-agents-py
pip install -e .
cp .env.example .env
```

Then fill `.env`:

- Required for workflow execution: `WORKFLOW_API_BASE`, `WORKFLOW_API_KEY`, `WORKFLOW_MODEL`.
- Optional for control-plane mode: `SIMPLEFLOW_API_BASE_URL` plus machine creds (`SIMPLEFLOW_CLIENT_ID`, `SIMPLEFLOW_CLIENT_SECRET`) or `SIMPLEFLOW_API_TOKEN`.
- Add `SIMPLEFLOW_AGENT_CATALOG_JSON` so UI onboarding knows which agent/runtime mapping to use.

## Run

CLI mode:

```bash
python scripts/chat_agent.py --workflow workflows/python-intern-fun-interview-chat.yaml
```

Type `exit` to quit.

Web mode:

```bash
uvicorn app:app --host 0.0.0.0 --port 8091 --reload
```

Open `http://localhost:8091`.

Docker mode (reads `.env` from project root):

```bash
make up
```

Stop container:

```bash
make down
```

Other useful commands:

- `make logs` - follow container logs.
- `make ps` - show container status.

## Control-plane endpoints

When `SIMPLEFLOW_API_BASE_URL` is configured, backend exposes:

- `POST /api/control-plane/sign-in`
- `DELETE /api/control-plane/sign-out`
- `GET /api/control-plane/me`
- `GET /api/control-plane/registration/preflight`
- `GET /api/control-plane/chat/sessions`
- `GET/POST/PATCH /api/control-plane/chat/messages`
- `POST /api/control-plane/chat/invoke`
- `GET /api/agents/available`
- `POST /api/onboarding/start`
- `GET /api/onboarding/status`
- `POST /api/onboarding/retry`

## Notes

- Workflow mode and control-plane mode can be used from the same frontend.
- Each web session is isolated and closes automatically on terminal interview decisions.
- Onboarding uses SDK runtime registration -> validate -> activate lifecycle with machine auth.
- Docker Compose uses `env_file: .env`, so the container picks configuration from the repository root `.env` file.
