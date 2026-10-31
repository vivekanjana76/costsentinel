"""The A2A HTTP surface, off by default.

Mounted on the existing FastAPI app when ``A2A_ENABLED=true``, and that is the whole of
"opt-in": no new dependency, no second process, and nothing listening unless an
operator said so.

Routes: reads, plus a create and a cancel of read tasks
-------------------------------------------------------

- ``GET /.well-known/agent-card.json``: the card, where A2A v1 says to look. Served as
  the exact JSON that was signed, with absent fields omitted, because a verifier
  canonicalises what it received.
- ``GET /.well-known/agent-card-jwks.json``: the public key the card's ``jku`` names.
- ``POST /a2a/tasks``: create a task for one advertised skill. Every skill reads. The
  task is returned *submitted* and runs after the response is sent.
- ``GET /a2a/tasks/{id}`` and ``GET /a2a/tasks/{id}/artifact``: poll, and collect.
- ``POST /a2a/tasks/{id}:cancel``: cancel a task that has not started. The path follows
  the spec's own ``tasks/{id}:cancel`` form.

The two POSTs change a *task*, an in-memory record this surface owns. Neither changes
a sweep, a checkpoint, a decision or a resource, and a test walks the routes to say so
(D130).
"""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from costsentinel import __version__
from costsentinel.a2a.card import AgentCard, build_card
from costsentinel.a2a.signing import (
    HMAC_HEADER,
    JWKS_PATH,
    card_hmac,
    jwks,
    load_key,
    sign_card,
    synthetic_key,
)
from costsentinel.a2a.tasks import (
    Artifact,
    Task,
    TaskError,
    TaskNotCancelableError,
    TaskStore,
    UnknownSkillError,
    UnknownTaskError,
)
from costsentinel.config import RunMode, Settings
from costsentinel.observability.logging import get_logger

_log = get_logger("a2a.server")

#: Where A2A v1 consumers look for a card.
CARD_PATH = "/.well-known/agent-card.json"

#: Where tasks are created.
TASKS_PATH = "/a2a/tasks"


class SubmitRequest(BaseModel):
    """A request to run one advertised skill."""

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(description="One of the skill ids on the card.")
    client: str | None = Field(
        default=None, description="Narrow to one client. Omit for every client in scope."
    )


def signing_key(settings: Settings) -> ec.EllipticCurvePrivateKey | None:
    """The key the card's JWS is made with, or ``None`` for an unsigned card.

    A configured file signs in any mode. Mock mode with none uses the derived synthetic
    key, so CI signs and verifies on every run. Real mode with none signs nothing,
    because a signature anybody can forge invites a consumer to trust it.
    """
    if settings.a2a_jws_key_file is not None:
        return load_key(settings.a2a_jws_key_file)
    return synthetic_key() if settings.mode is RunMode.MOCK else None


def jku_for(settings: Settings) -> str:
    """The JWKS URL the protected header names."""
    return f"{settings.a2a_url.rstrip('/')}{JWKS_PATH}"


def card_for(settings: Settings, key: ec.EllipticCurvePrivateKey | None = None) -> AgentCard:
    """Build this deployment's card, signed when there is a key."""
    card = build_card(url=settings.a2a_url, version=__version__)
    chosen = key if key is not None else signing_key(settings)
    return sign_card(card, chosen, jku=jku_for(settings)) if chosen is not None else card


def card_document(settings: Settings) -> dict[str, Any]:
    """The card exactly as it is served, for a CLI to print or a file to hold."""
    return card_for(settings).wire()


def build_router(settings: Settings, store: TaskStore | None = None) -> APIRouter:
    """The A2A routes, bound to one task store and one signing key.

    The store can be injected, so a test can drive the lifecycle in-process with no
    network at all. That is how CI exercises this. The key is loaded once, here, so a
    misconfigured key file fails while the app is built and not on the first card
    request.
    """
    router = APIRouter(tags=["a2a"])
    tasks: TaskStore = store or TaskStore(settings)
    key = signing_key(settings)
    hmac_secret = settings.a2a_signing_key.get_secret_value() if settings.a2a_signing_key else None

    @router.get(CARD_PATH)
    def agent_card() -> JSONResponse:
        """Publish the Agent Card. Read-only, and it says so on the card."""
        served = card_for(settings, key).wire()
        headers = {HMAC_HEADER: card_hmac(served, hmac_secret)} if hmac_secret else {}
        return JSONResponse(served, headers=headers)

    @router.get(JWKS_PATH)
    def card_keys() -> dict[str, Any]:
        """The public key the card's signature verifies under. Empty when unsigned."""
        return jwks(key) if key is not None else {"keys": []}

    @router.post(TASKS_PATH, response_model=Task, status_code=status.HTTP_201_CREATED)
    def submit(request: SubmitRequest, background: BackgroundTasks) -> Task:
        """Create a task for one advertised skill. It runs after this response is sent."""
        try:
            task = tasks.submit(request.skill, {"client": request.client})
        except UnknownSkillError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        background.add_task(tasks.run, task.task_id)
        return task

    @router.get(f"{TASKS_PATH}/{{task_id}}", response_model=Task)
    def task_status(task_id: str) -> Task:
        """Where one task has got to."""
        try:
            return tasks.status(task_id)
        except UnknownTaskError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    @router.get(f"{TASKS_PATH}/{{task_id}}/artifact", response_model=Artifact)
    def task_artifact(task_id: str) -> Any:
        """What one completed task produced, as its skill's typed artefact."""
        try:
            return tasks.artifact(task_id)
        except UnknownTaskError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except TaskError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    @router.post(f"{TASKS_PATH}/{{task_id}}:cancel", response_model=Task)
    def task_cancel(task_id: str) -> Task:
        """Cancel a task before its read starts. Idempotent on a cancelled task."""
        try:
            return tasks.cancel(task_id)
        except UnknownTaskError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except TaskNotCancelableError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return router


def mount(app: FastAPI, settings: Settings, store: TaskStore | None = None) -> bool:
    """Attach the A2A routes if this deployment enables them.

    Returns:
        Whether they were attached, so the caller can log it rather than infer it.
    """
    if not settings.a2a_enabled:
        _log.info("a2a surface disabled")
        return False

    app.include_router(build_router(settings, store))
    _log.info(
        "a2a surface enabled",
        extra={
            "url": settings.a2a_url,
            "jws_signed": signing_key(settings) is not None,
            "hmac_fallback": settings.a2a_signing_key is not None,
        },
    )
    return True
