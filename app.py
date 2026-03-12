from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from simple_agents_py import Client
from simpleflow_sdk import ChatMessageWrite, SimpleFlowClient


logger = logging.getLogger("simpleagent-interview-system")


@dataclass
class SessionState:
    messages: list[dict[str, str]]
    closed: bool


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ControlPlaneSignInRequest(BaseModel):
    email: str
    password: str


class OnboardingStartRequest(BaseModel):
    agent_id: str
    agent_version: str = ""


class OnboardingRetryRequest(BaseModel):
    agent_id: str
    agent_version: str = ""


@dataclass(slots=True)
class AgentCatalogEntry:
    agent_id: str
    agent_version: str
    org_id: str
    runtime_endpoint_url: str
    runtime_id: str
    enabled: bool


@dataclass(slots=True)
class OnboardingStepState:
    name: str
    status: Literal["pending", "running", "success", "failed"]
    attempts: int
    last_error: str
    updated_at_ms: int


@dataclass(slots=True)
class OnboardingRecord:
    onboarding_id: str
    idempotency_key: str
    overall_status: Literal["not_started", "in_progress", "active", "blocked", "failed"]
    agent: AgentCatalogEntry
    registration_id: str
    created_at_ms: int
    updated_at_ms: int
    last_error: str
    steps: dict[str, OnboardingStepState]


def _load_workflow_config() -> tuple[str, str, str, str, str]:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    load_dotenv()

    provider = os.getenv("WORKFLOW_PROVIDER", "openai").strip()
    configured_api_base = os.getenv("WORKFLOW_API_BASE", "").strip()
    api_base = _normalize_localhost_url_for_container(configured_api_base)
    api_key = os.getenv("WORKFLOW_API_KEY", "").strip()
    model = os.getenv("WORKFLOW_MODEL", "").strip()
    workflow_path = os.getenv(
        "WORKFLOW_PATH", "workflows/python-intern-fun-interview-chat.yaml"
    ).strip()

    if api_base == "" or api_key == "":
        raise RuntimeError(
            "Set WORKFLOW_API_BASE and WORKFLOW_API_KEY in .env before starting the app."
        )

    return provider, api_base, api_key, model, workflow_path


def _load_control_plane_config() -> dict[str, str]:
    configured_base_url = os.getenv("SIMPLEFLOW_API_BASE_URL", "").strip()
    effective_base_url = _normalize_localhost_url_for_container(configured_base_url)
    return {
        "base_url": effective_base_url,
        "configured_base_url": configured_base_url,
        "api_token": os.getenv("SIMPLEFLOW_API_TOKEN", "").strip(),
        "client_id": os.getenv("SIMPLEFLOW_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("SIMPLEFLOW_CLIENT_SECRET", "").strip(),
    }


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists()


def _normalize_localhost_url_for_container(raw_url: str) -> str:
    if raw_url == "":
        return ""

    parsed = urlparse(raw_url)
    host = parsed.hostname
    if _running_in_container() and host in {"localhost", "127.0.0.1"}:
        port = parsed.port
        netloc = "host.docker.internal"
        if port is not None:
            netloc = f"{netloc}:{port}"
        parsed = parsed._replace(netloc=netloc)
        return urlunparse(parsed)

    return raw_url


def _resolve_workflow_path(raw_path: str) -> Path:
    root = Path(__file__).resolve().parent
    candidate = Path(raw_path)
    if candidate.exists():
        return candidate

    local = root / raw_path
    if local.exists():
        return local

    raise RuntimeError(f"Workflow file not found: {raw_path}")


def _render_reply(terminal_output: Any) -> str:
    if isinstance(terminal_output, str):
        return terminal_output
    if not isinstance(terminal_output, dict):
        return json.dumps(terminal_output, ensure_ascii=True)

    for key in ("message", "question", "feedback", "reason"):
        value = terminal_output.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()

    return json.dumps(terminal_output, ensure_ascii=True)


def _is_closed(terminal_node: Any, terminal_output: Any) -> bool:
    if isinstance(terminal_node, str) and terminal_node in {
        "terminate_candidate",
        "already_terminated",
    }:
        return True

    if isinstance(terminal_output, dict):
        decision = terminal_output.get("decision")
        if isinstance(decision, str) and decision.strip().lower() == "terminated":
            return True

    return False


def _new_session_messages() -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are an interview assistant. Keep it structured, fair, and ask one question at a time."
            ),
        }
    ]


def _extract_bearer_token(request: Request, *, detail: str) -> str:
    authorization = request.headers.get("authorization", "").strip()
    if authorization == "":
        raise HTTPException(status_code=401, detail=detail)
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].strip().lower() != "bearer":
        raise HTTPException(status_code=401, detail=detail)
    token = parts[1].strip()
    if token == "":
        raise HTTPException(status_code=401, detail=detail)
    return token


def _agent_key(agent_id: str, agent_version: str) -> str:
    return f"{agent_id}::{agent_version}"


def _bool_from_value(value: Any, fallback: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    normalized = str(value).strip().lower()
    if normalized == "":
        return fallback
    return normalized in {"1", "true", "yes", "on"}


def _to_agent_catalog_entry(item: Any) -> AgentCatalogEntry | None:
    if not isinstance(item, dict):
        return None

    normalized_agent_id = str(item.get("agent_id", "")).strip()
    normalized_agent_version = str(item.get("agent_version", "")).strip() or "v1"
    normalized_org_id = str(item.get("org_id", "")).strip()
    normalized_endpoint = str(item.get("runtime_endpoint_url", "")).strip()
    normalized_runtime_id = str(item.get("runtime_id", "")).strip()
    enabled = _bool_from_value(item.get("enabled", True), True)

    if normalized_agent_id == "":
        return None

    return AgentCatalogEntry(
        agent_id=normalized_agent_id,
        agent_version=normalized_agent_version,
        org_id=normalized_org_id,
        runtime_endpoint_url=normalized_endpoint,
        runtime_id=normalized_runtime_id,
        enabled=enabled,
    )


def _load_agent_catalog() -> dict[str, AgentCatalogEntry]:
    catalog_json = os.getenv("SIMPLEFLOW_AGENT_CATALOG_JSON", "").strip()
    catalog: dict[str, AgentCatalogEntry] = {}

    if catalog_json != "":
        try:
            parsed = json.loads(catalog_json)
            if isinstance(parsed, list):
                for item in parsed:
                    entry = _to_agent_catalog_entry(item)
                    if entry is None:
                        continue
                    catalog[_agent_key(entry.agent_id, entry.agent_version)] = entry
        except json.JSONDecodeError:
            pass

    if len(catalog) == 0:
        fallback = AgentCatalogEntry(
            agent_id=os.getenv("RUNTIME_AGENT_ID", "hr-agent").strip() or "hr-agent",
            agent_version=os.getenv("RUNTIME_AGENT_VERSION", "v1").strip() or "v1",
            org_id=os.getenv("RUNTIME_ORG_ID", "").strip(),
            runtime_endpoint_url=os.getenv(
                "RUNTIME_BOOTSTRAP_ENDPOINT_URL", ""
            ).strip(),
            runtime_id=os.getenv("RUNTIME_BOOTSTRAP_RUNTIME_ID", "").strip(),
            enabled=True,
        )
        catalog[_agent_key(fallback.agent_id, fallback.agent_version)] = fallback

    return catalog


def _normalize_control_plane_me(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    user_candidate = payload.get("user")
    user_payload: dict[str, Any] = (
        user_candidate if isinstance(user_candidate, dict) else {}
    )

    def _first_non_empty(*values: Any) -> str:
        for value in values:
            normalized = str(value).strip() if value is not None else ""
            if normalized != "":
                return normalized
        return ""

    user_id = _first_non_empty(
        payload.get("id"),
        payload.get("user_id"),
        payload.get("userId"),
        user_payload.get("id"),
        user_payload.get("user_id"),
        user_payload.get("userId"),
    )
    organization_id = _first_non_empty(
        payload.get("organization_id"),
        payload.get("organizationId"),
        user_payload.get("organization_id"),
        user_payload.get("organizationId"),
    )

    normalized = dict(payload)
    if user_id != "":
        normalized["id"] = user_id
        normalized["user_id"] = user_id
    if organization_id != "":
        normalized["organization_id"] = organization_id
    return normalized


def _request_base_url(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").strip()
    if forwarded_proto != "" and forwarded_host != "":
        return f"{forwarded_proto}://{forwarded_host}".rstrip("/")

    parsed = request.url
    scheme = parsed.scheme.strip()
    netloc = parsed.netloc.strip()
    if scheme != "" and netloc != "":
        return f"{scheme}://{netloc}".rstrip("/")
    return ""


def _ensure_agent_runtime_endpoint(
    request: Request, agent: AgentCatalogEntry
) -> AgentCatalogEntry:
    existing = agent.runtime_endpoint_url.strip()
    if existing != "":
        return agent

    configured_public_base = (
        os.getenv("RUNTIME_PUBLIC_BASE_URL", "").strip().rstrip("/")
    )
    if configured_public_base != "":
        return replace(agent, runtime_endpoint_url=configured_public_base)

    inferred_base = _request_base_url(request)
    if inferred_base != "":
        return replace(agent, runtime_endpoint_url=inferred_base)

    return agent


def _sdk_error_to_http_exception(exc: Exception) -> HTTPException:
    text = str(exc)
    if "status=401" in text:
        return HTTPException(
            status_code=401, detail="control-plane request unauthorized"
        )
    if "status=403" in text:
        return HTTPException(status_code=403, detail="control-plane request forbidden")
    if "status=400" in text:
        return HTTPException(status_code=400, detail="invalid control-plane request")
    return HTTPException(
        status_code=502, detail=f"control-plane request failed: {text}"
    )


def _ensure_simpleflow_client() -> Any:
    if simpleflow_client is None:
        raise HTTPException(
            status_code=503,
            detail="control-plane client unavailable; set SIMPLEFLOW_API_BASE_URL and install simpleflow-sdk",
        )
    return simpleflow_client


def _runtime_write_client() -> Any:
    if runtime_write_client is not None:
        return runtime_write_client
    return simpleflow_client


def _default_telemetry_agent() -> AgentCatalogEntry | None:
    enabled_entries = [
        entry for entry in agent_catalog.values() if entry.enabled is True
    ]
    if len(enabled_entries) == 0:
        return None
    enabled_entries.sort(key=lambda item: (item.agent_id, item.agent_version))
    return enabled_entries[0]


def _emit_runtime_telemetry_for_chat(
    *,
    session_id: str,
    user_message: str,
    assistant_reply: str,
    workflow_result: Any,
) -> None:
    if not isinstance(workflow_result, dict):
        return
    write_client = _runtime_write_client()
    if write_client is None:
        return

    agent = _default_telemetry_agent()
    if agent is None:
        return

    run_id = str(workflow_result.get("run_id", "")).strip()
    if run_id == "":
        run_id = session_id

    event_counts: dict[str, int] = {}
    events = workflow_result.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type", "")).strip()
            if event_type == "":
                continue
            prior = event_counts.get(event_type, 0)
            event_counts[event_type] = prior + 1

    message_id = f"assistant-{uuid.uuid4().hex}"
    chat_idempotency_key = f"runtime-chat-{message_id}"

    try:
        write_client.write_event_from_workflow_result(
            agent_id=agent.agent_id,
            workflow_result=workflow_result,
            event_type="runtime.invoke.completed",
            organization_id=agent.org_id,
            user_id=session_id,
            include_raw=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("runtime telemetry event write failed: %s", exc)

    try:
        write_client.write_chat_message(
            ChatMessageWrite(
                agent_id=agent.agent_id,
                organization_id=agent.org_id,
                run_id=run_id,
                role="user",
                direction="inbound",
                chat_id=session_id,
                message_id=f"user-{uuid.uuid4().hex}",
                content={"text": user_message},
                metadata={
                    "source": "local-workflow",
                    "event_counts": event_counts,
                },
            )
        )

        write_from_workflow = getattr(
            write_client, "write_chat_message_from_workflow_result", None
        )
        if callable(write_from_workflow):
            write_from_workflow(
                agent_id=agent.agent_id,
                organization_id=agent.org_id,
                run_id=run_id,
                role="assistant",
                workflow_result=workflow_result,
                trace_id="",
                span_id=run_id,
                tenant_id=agent.org_id,
                chat_id=session_id,
                message_id=message_id,
                direction="outbound",
                created_at_ms=int(time.time() * 1000),
                idempotency_key=chat_idempotency_key,
            )
        else:
            write_client.write_chat_message(
                ChatMessageWrite(
                    agent_id=agent.agent_id,
                    organization_id=agent.org_id,
                    run_id=run_id,
                    role="assistant",
                    direction="outbound",
                    chat_id=session_id,
                    message_id=message_id,
                    content={"text": assistant_reply},
                    metadata={
                        "source": "local-workflow",
                        "event_counts": event_counts,
                    },
                    idempotency_key=chat_idempotency_key,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("runtime chat message write failed: %s", exc)


def _require_operator_session(request: Request) -> str:
    token = _extract_bearer_token(request, detail="unauthorized request")
    client_ref = _ensure_simpleflow_client()
    try:
        client_ref.get_me(auth_token=token)
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc
    return token


def _resolve_agent_or_404(
    requested_agent_id: str, requested_agent_version: str
) -> AgentCatalogEntry:
    normalized_agent_id = requested_agent_id.strip()
    normalized_agent_version = requested_agent_version.strip()

    if normalized_agent_id == "":
        raise HTTPException(status_code=400, detail="agent_id is required")

    if normalized_agent_version == "":
        for entry in agent_catalog.values():
            if entry.agent_id == normalized_agent_id:
                normalized_agent_version = entry.agent_version
                break

    lookup_key = _agent_key(normalized_agent_id, normalized_agent_version)
    entry = agent_catalog.get(lookup_key)
    if entry is None:
        for candidate in agent_catalog.values():
            if candidate.agent_id == normalized_agent_id:
                entry = candidate
                break

    if entry is None:
        enabled_entries = [
            candidate for candidate in agent_catalog.values() if candidate.enabled
        ]
        if len(enabled_entries) == 1:
            entry = enabled_entries[0]

    if entry is None:
        available_ids = sorted(
            {candidate.agent_id for candidate in agent_catalog.values()}
        )
        raise HTTPException(
            status_code=404,
            detail=(
                "agent catalog entry not found"
                if len(available_ids) == 0
                else f"agent catalog entry not found; available agent_id values: {', '.join(available_ids)}"
            ),
        )
    if entry.enabled is False:
        raise HTTPException(status_code=409, detail="agent catalog entry is disabled")
    return entry


def _step_state(
    name: str, status: Literal["pending", "running", "success", "failed"]
) -> OnboardingStepState:
    now_ms = int(time.time() * 1000)
    return OnboardingStepState(
        name=name, status=status, attempts=0, last_error="", updated_at_ms=now_ms
    )


def _new_onboarding_record(agent: AgentCatalogEntry) -> OnboardingRecord:
    now_ms = int(time.time() * 1000)
    material = "|".join(
        [
            agent.org_id,
            agent.agent_id,
            agent.agent_version,
            agent.runtime_id,
            agent.runtime_endpoint_url,
        ]
    )
    idempotency_key = uuid.uuid5(uuid.NAMESPACE_DNS, material).hex
    return OnboardingRecord(
        onboarding_id=f"onb_{uuid.uuid4().hex}",
        idempotency_key=idempotency_key,
        overall_status="not_started",
        agent=agent,
        registration_id="",
        created_at_ms=now_ms,
        updated_at_ms=now_ms,
        last_error="",
        steps={
            "create": _step_state("create", "pending"),
            "validate": _step_state("validate", "pending"),
            "activate": _step_state("activate", "pending"),
        },
    )


def _serialize_onboarding(record: OnboardingRecord) -> dict[str, Any]:
    return {
        "onboarding_id": record.onboarding_id,
        "idempotency_key": record.idempotency_key,
        "overall_status": record.overall_status,
        "agent": {
            "agent_id": record.agent.agent_id,
            "agent_version": record.agent.agent_version,
            "org_id": record.agent.org_id,
            "runtime_endpoint_url": record.agent.runtime_endpoint_url,
            "runtime_id": record.agent.runtime_id,
            "enabled": record.agent.enabled,
        },
        "registration_id": record.registration_id,
        "last_error": record.last_error,
        "created_at_ms": record.created_at_ms,
        "updated_at_ms": record.updated_at_ms,
        "steps": [
            {
                "name": step.name,
                "status": step.status,
                "attempts": step.attempts,
                "last_error": step.last_error,
                "updated_at_ms": step.updated_at_ms,
            }
            for step in record.steps.values()
        ],
    }


def _set_step_running(record: OnboardingRecord, step_name: str) -> None:
    step = record.steps[step_name]
    step.status = "running"
    step.attempts = step.attempts + 1
    step.last_error = ""
    step.updated_at_ms = int(time.time() * 1000)
    record.overall_status = "in_progress"
    record.updated_at_ms = step.updated_at_ms
    record.last_error = ""


def _set_step_success(record: OnboardingRecord, step_name: str) -> None:
    step = record.steps[step_name]
    step.status = "success"
    step.last_error = ""
    step.updated_at_ms = int(time.time() * 1000)
    record.updated_at_ms = step.updated_at_ms


def _set_step_failed(record: OnboardingRecord, step_name: str, error: str) -> None:
    step = record.steps[step_name]
    step.status = "failed"
    step.last_error = error
    step.updated_at_ms = int(time.time() * 1000)
    record.overall_status = "failed"
    record.last_error = error
    record.updated_at_ms = step.updated_at_ms


def _extract_registration_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("id", "ID", "registration_id", "registrationId", "RegistrationID"):
            value = str(payload.get(key, "")).strip()
            if value != "":
                return value
    return ""


def _run_onboarding_lifecycle(
    record: OnboardingRecord, operator_auth_token: str
) -> None:
    client_ref = _ensure_simpleflow_client()
    ordered_steps = ["create", "validate", "activate"]

    endpoint_url = record.agent.runtime_endpoint_url.strip()
    if endpoint_url == "":
        _set_step_failed(
            record,
            "create",
            "runtime_endpoint_url is required for runtime registration",
        )
        return

    for step_name in ordered_steps:
        step = record.steps[step_name]
        if step.status == "success":
            continue

        _set_step_running(record, step_name)
        try:
            if step_name == "create":
                registration_payload: dict[str, Any] = {
                    "agent_id": record.agent.agent_id,
                    "agent_version": record.agent.agent_version,
                    "execution_mode": "remote_runtime",
                    "endpoint_url": endpoint_url,
                    "auth_mode": "jwt",
                    "capabilities": ["chat"],
                }
                runtime_id = record.agent.runtime_id.strip()
                if runtime_id != "":
                    registration_payload["runtime_id"] = runtime_id
                created = client_ref.register_runtime(
                    registration_payload,
                    auth_token=operator_auth_token,
                )
                registration_id = _extract_registration_id(created)
                if registration_id != "":
                    record.registration_id = registration_id
            elif step_name == "validate":
                if record.registration_id == "":
                    raise HTTPException(
                        status_code=502,
                        detail="registration id is required before validation",
                    )
                client_ref.validate_runtime_registration(
                    record.registration_id,
                    auth_token=operator_auth_token,
                )
            else:
                if record.registration_id == "":
                    raise HTTPException(
                        status_code=502,
                        detail="registration id is required before activation",
                    )
                client_ref.activate_runtime_registration(
                    record.registration_id,
                    auth_token=operator_auth_token,
                )
            _set_step_success(record, step_name)
        except HTTPException as exc:
            _set_step_failed(record, step_name, str(exc.detail))
            return
        except Exception as exc:  # noqa: BLE001
            _set_step_failed(record, step_name, str(exc))
            return

    record.overall_status = "active"
    record.last_error = ""
    record.updated_at_ms = int(time.time() * 1000)


def _first_failed_step(record: OnboardingRecord) -> str:
    for name in ["create", "validate", "activate"]:
        if record.steps[name].status == "failed":
            return name
    return ""


def _reset_steps_for_retry(record: OnboardingRecord, from_step: str) -> None:
    ordered_steps = ["create", "validate", "activate"]
    start_index = ordered_steps.index(from_step)
    for index, step_name in enumerate(ordered_steps):
        if index >= start_index:
            step = record.steps[step_name]
            step.status = "pending"
            step.last_error = ""
            step.updated_at_ms = int(time.time() * 1000)


provider, api_base, api_key, default_model, workflow_path_raw = _load_workflow_config()
workflow_path = _resolve_workflow_path(workflow_path_raw)
client = Client(provider, api_base=api_base, api_key=api_key)

control_plane_config = _load_control_plane_config()
if SimpleFlowClient is not None and control_plane_config["base_url"] != "":
    simpleflow_client: Any = SimpleFlowClient(
        control_plane_config["base_url"],
        api_token=control_plane_config["api_token"] or None,
        oauth_client_id=control_plane_config["client_id"] or None,
        oauth_client_secret=control_plane_config["client_secret"] or None,
    )
else:
    simpleflow_client = None

if (
    SimpleFlowClient is not None
    and control_plane_config["base_url"] != ""
    and control_plane_config["client_id"] != ""
    and control_plane_config["client_secret"] != ""
):
    runtime_write_client: Any = SimpleFlowClient(
        control_plane_config["base_url"],
        api_token=None,
        oauth_client_id=control_plane_config["client_id"],
        oauth_client_secret=control_plane_config["client_secret"],
    )
else:
    runtime_write_client = None

agent_catalog = _load_agent_catalog()

app = FastAPI(title="SimpleAgentInterViewSystem")
root_dir = Path(__file__).resolve().parent
frontend_dir = root_dir / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

sessions: dict[str, SessionState] = {}
sessions_lock = threading.Lock()

onboarding_lock = threading.Lock()
onboarding_state_by_agent: dict[str, OnboardingRecord] = {}
onboarding_state_by_id: dict[str, OnboardingRecord] = {}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "simpleagent-interview-system"}


@app.get("/api/control-plane/health")
def control_plane_health() -> dict[str, Any]:
    configured = simpleflow_client is not None
    return {
        "configured": configured,
        "base_url": control_plane_config["base_url"],
        "configured_base_url": control_plane_config["configured_base_url"],
        "has_machine_credentials": control_plane_config["client_id"] != ""
        and control_plane_config["client_secret"] != "",
        "has_runtime_write_client": runtime_write_client is not None,
        "workflow_api_base": api_base,
        "catalog_size": len(agent_catalog),
    }


@app.get("/")
def home() -> FileResponse:
    return FileResponse(str(frontend_dir / "index.html"))


@app.post("/api/control-plane/sign-in")
def control_plane_sign_in(payload: ControlPlaneSignInRequest) -> Any:
    client_ref = _ensure_simpleflow_client()
    email = str(payload.email).strip()
    password = str(payload.password).strip()
    if email == "" or password == "":
        raise HTTPException(status_code=400, detail="email and password are required")
    try:
        return client_ref.create_session(email=email, password=password)
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc


@app.delete("/api/control-plane/sign-out")
def control_plane_sign_out(request: Request) -> dict[str, bool]:
    client_ref = _ensure_simpleflow_client()
    token = _extract_bearer_token(request, detail="unauthorized sign-out request")
    try:
        client_ref.delete_current_session(auth_token=token)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc


@app.get("/api/control-plane/me")
def control_plane_me(request: Request) -> Any:
    client_ref = _ensure_simpleflow_client()
    token = _extract_bearer_token(request, detail="unauthorized me request")
    try:
        payload = client_ref.get_me(auth_token=token)
        return _normalize_control_plane_me(payload)
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc


@app.get("/api/agents/available")
def available_agents(request: Request) -> dict[str, Any]:
    _require_operator_session(request)
    agents = [
        {
            "agent_id": entry.agent_id,
            "agent_version": entry.agent_version,
            "org_id": entry.org_id,
            "runtime_endpoint_url": _ensure_agent_runtime_endpoint(
                request, entry
            ).runtime_endpoint_url,
            "runtime_id": entry.runtime_id,
            "enabled": entry.enabled,
        }
        for entry in agent_catalog.values()
        if entry.enabled is True
    ]
    agents.sort(key=lambda item: (item["agent_id"], item["agent_version"]))
    return {"agents": agents}


@app.post("/api/onboarding/start")
def onboarding_start(
    payload: OnboardingStartRequest, request: Request
) -> dict[str, Any]:
    operator_token = _require_operator_session(request)
    agent = _resolve_agent_or_404(payload.agent_id, payload.agent_version)
    agent = _ensure_agent_runtime_endpoint(request, agent)
    key = _agent_key(agent.agent_id, agent.agent_version)

    with onboarding_lock:
        existing = onboarding_state_by_agent.get(key)
        if existing is None:
            existing = _new_onboarding_record(agent)
            onboarding_state_by_agent[key] = existing
            onboarding_state_by_id[existing.onboarding_id] = existing

        _run_onboarding_lifecycle(existing, operator_token)
        return _serialize_onboarding(existing)


@app.get("/api/onboarding/status")
def onboarding_status(
    request: Request, onboarding_id: str = "", agent_id: str = ""
) -> dict[str, Any]:
    _require_operator_session(request)
    normalized_onboarding_id = onboarding_id.strip()
    normalized_agent_id = agent_id.strip()

    with onboarding_lock:
        if normalized_onboarding_id != "":
            record = onboarding_state_by_id.get(normalized_onboarding_id)
            if record is None:
                raise HTTPException(
                    status_code=404, detail="onboarding record not found"
                )
            return _serialize_onboarding(record)

        if normalized_agent_id != "":
            candidates = [
                record
                for record in onboarding_state_by_agent.values()
                if record.agent.agent_id == normalized_agent_id
            ]
            if len(candidates) == 0:
                raise HTTPException(
                    status_code=404, detail="onboarding record not found"
                )
            candidates.sort(key=lambda item: item.updated_at_ms, reverse=True)
            return _serialize_onboarding(candidates[0])

    raise HTTPException(
        status_code=400, detail="onboarding_id or agent_id query parameter is required"
    )


@app.post("/api/onboarding/retry")
def onboarding_retry(
    payload: OnboardingRetryRequest, request: Request
) -> dict[str, Any]:
    operator_token = _require_operator_session(request)
    agent = _resolve_agent_or_404(payload.agent_id, payload.agent_version)
    agent = _ensure_agent_runtime_endpoint(request, agent)
    key = _agent_key(agent.agent_id, agent.agent_version)

    with onboarding_lock:
        existing = onboarding_state_by_agent.get(key)
        if existing is None:
            existing = _new_onboarding_record(agent)
            onboarding_state_by_agent[key] = existing
            onboarding_state_by_id[existing.onboarding_id] = existing

        failed_step = _first_failed_step(existing)
        if failed_step == "":
            failed_step = "create"
            if existing.steps["create"].status == "success":
                failed_step = "validate"
            if existing.steps["validate"].status == "success":
                failed_step = "activate"

        _reset_steps_for_retry(existing, failed_step)
        _run_onboarding_lifecycle(existing, operator_token)
        return _serialize_onboarding(existing)


@app.get("/api/control-plane/registration/preflight")
def control_plane_registration_preflight(
    request: Request, agent_id: str = "", agent_version: str = ""
) -> Any:
    client_ref = _ensure_simpleflow_client()
    token = _extract_bearer_token(
        request, detail="unauthorized registration preflight request"
    )
    normalized_agent_id = agent_id.strip()
    normalized_agent_version = agent_version.strip()
    if normalized_agent_id == "" or normalized_agent_version == "":
        raise HTTPException(
            status_code=400, detail="agent_id and agent_version are required"
        )
    try:
        registrations = client_ref.list_runtime_registrations(
            agent_id=normalized_agent_id,
            agent_version=normalized_agent_version,
            auth_token=token,
        )
        return {"registrations": registrations}
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc


@app.get("/api/control-plane/chat/sessions")
def control_plane_chat_sessions(
    request: Request,
    agent_id: str = "",
    user_id: str = "",
    status: str = "active",
    limit: int = 50,
) -> Any:
    client_ref = _ensure_simpleflow_client()
    token = _extract_bearer_token(request, detail="unauthorized chat sessions request")
    if agent_id.strip() == "" or user_id.strip() == "":
        raise HTTPException(status_code=400, detail="agent_id and user_id are required")
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be greater than zero")
    try:
        sessions_response = client_ref.list_chat_sessions(
            agent_id=agent_id.strip(),
            user_id=user_id.strip(),
            status=status.strip() or "active",
            limit=limit,
            auth_token=token,
        )
        return {"sessions": sessions_response}
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc


@app.get("/api/control-plane/chat/messages")
def control_plane_chat_messages(
    request: Request,
    agent_id: str = "",
    chat_id: str = "",
    user_id: str = "",
    limit: int = 50,
) -> Any:
    client_ref = _ensure_simpleflow_client()
    token = _extract_bearer_token(request, detail="unauthorized chat messages request")
    if agent_id.strip() == "" or chat_id.strip() == "" or user_id.strip() == "":
        raise HTTPException(
            status_code=400, detail="agent_id, chat_id, and user_id are required"
        )
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be greater than zero")
    try:
        messages = client_ref.list_chat_history_messages(
            agent_id=agent_id.strip(),
            chat_id=chat_id.strip(),
            user_id=user_id.strip(),
            limit=limit,
            auth_token=token,
        )
        return {"messages": messages}
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc


@app.post("/api/control-plane/chat/messages")
def control_plane_create_chat_message(request: Request, payload: dict[str, Any]) -> Any:
    client_ref = _ensure_simpleflow_client()
    token = _extract_bearer_token(
        request, detail="unauthorized chat message create request"
    )
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid chat message payload")
    try:
        return client_ref.create_chat_history_message(payload, auth_token=token)
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc


@app.patch("/api/control-plane/chat/messages/{message_id}")
def control_plane_patch_chat_message(
    message_id: str, request: Request, payload: dict[str, Any]
) -> Any:
    client_ref = _ensure_simpleflow_client()
    token = _extract_bearer_token(
        request, detail="unauthorized chat message update request"
    )
    normalized_message_id = message_id.strip()
    if normalized_message_id == "":
        raise HTTPException(status_code=400, detail="message_id is required")
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="invalid chat message update payload"
        )

    agent_id = str(payload.get("agent_id", "")).strip()
    chat_id = str(payload.get("chat_id", "")).strip()
    user_id = str(payload.get("user_id", "")).strip()
    content = payload.get("content", {})
    metadata = payload.get("metadata", {})
    if agent_id == "" or chat_id == "" or user_id == "":
        raise HTTPException(
            status_code=400, detail="agent_id, chat_id, and user_id are required"
        )

    try:
        return client_ref.update_chat_history_message(
            message_id=normalized_message_id,
            agent_id=agent_id,
            chat_id=chat_id,
            user_id=user_id,
            content=content if isinstance(content, dict) else {},
            metadata=metadata if isinstance(metadata, dict) else {},
            auth_token=token,
        )
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc


@app.post("/api/control-plane/chat/invoke")
def control_plane_chat_invoke(request: Request, payload: dict[str, Any]) -> Any:
    client_ref = _ensure_simpleflow_client()
    token = _extract_bearer_token(request, detail="unauthorized chat invoke request")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid invoke payload")
    try:
        return client_ref.invoke(payload, auth_token=token)
    except Exception as exc:  # noqa: BLE001
        raise _sdk_error_to_http_exception(exc) from exc


@app.post("/api/session")
def create_session() -> dict[str, str]:
    session_id = f"sess_{uuid.uuid4().hex}"
    with sessions_lock:
        sessions[session_id] = SessionState(
            messages=_new_session_messages(), closed=False
        )
    return {"session_id": session_id}


@app.post("/api/chat")
def chat(payload: ChatRequest) -> dict[str, Any]:
    session_id = payload.session_id.strip()
    user_message = payload.message.strip()

    if session_id == "":
        raise HTTPException(status_code=400, detail="session_id is required")
    if user_message == "":
        raise HTTPException(status_code=400, detail="message is required")

    with sessions_lock:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if session.closed:
            raise HTTPException(
                status_code=409,
                detail="interview session is closed; start a new session",
            )
        session.messages.append({"role": "user", "content": user_message})
        workflow_input = {"messages": session.messages}

    workflow_options: dict[str, Any] = {}
    if default_model != "":
        workflow_options["model"] = default_model
    workflow_options["trace"] = {"tenant": {"run_id": session_id}}
    workflow_options["telemetry"] = {"nerdstats": True}

    try:
        result = client.run_workflow_yaml(
            str(workflow_path),
            workflow_input,
            include_events=True,
            workflow_options=workflow_options,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"workflow execution failed: {exc}"
        ) from exc

    terminal_output = (
        result.get("terminal_output") if isinstance(result, dict) else None
    )
    terminal_node = result.get("terminal_node") if isinstance(result, dict) else None
    assistant_reply = _render_reply(terminal_output)
    closed = _is_closed(terminal_node, terminal_output)

    with sessions_lock:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        session.messages.append({"role": "assistant", "content": assistant_reply})
        session.closed = closed

    _emit_runtime_telemetry_for_chat(
        session_id=session_id,
        user_message=user_message,
        assistant_reply=assistant_reply,
        workflow_result=result,
    )

    return {
        "session_id": session_id,
        "reply": assistant_reply,
        "terminal_node": terminal_node,
        "closed": closed,
    }
