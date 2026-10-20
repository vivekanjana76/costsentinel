"""The A2A HTTP surface, off by default.

Mounted on the existing FastAPI app when ``A2A_ENABLED=true``, which is the whole of
"opt-in" -- no new dependency, no second process, and nothing listening unless an
operator said so. The card itself is published either way: a consumer should be able to
discover what is *available* rather than probe for it, and a card describing a disabled
endpoint is honest in a way a 404 is not.

Four routes, all GET or a POST that reads
-----------------------------------------

``/.well-known/agent-card.json`` is where A2A v1 says to look. Submitting a task is a
POST because it creates one, and every task it can create is a read -- the three
advertised skills return a report, stored findings, or the pending-approval queue.
There is no route that decides an approval and none that changes a resource, which is
the same property the rest of the phase asserts four ways.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from costsentinel import __version__
from costsentinel.a2a.card import SYNTHETIC_SIGNING_KEY, AgentCard, build_card
from costsentinel.a2a.tasks import (
    Artifact,
    Task,
    TaskError,
    TaskStore,
    UnknownSkillError,
    UnknownTaskError,
)
from costsentinel.config import RunMode, Settings
from costsentinel.observability.logging import get_logger

_log = get_logger("a2a.server")

#: Where A2A v1 consumers look for a card.
CARD_PATH = "/.well-known/agent-card.json"


class SubmitRequest(BaseModel):
    """A request to run one advertised skill."""

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(description="One of the skill ids on the card.")
    client: str | None = Field(
        default=None, description="Narrow to one client. Omit for every client in scope."
    )


def signing_key(settings: Settings) -> str:
    """The key the card is signed with.

    The synthetic key in mock mode, so the signature can be verified in CI -- a card
    whose signature has never been checked is a card whose signing is untested. A real
    mode with no configured key produces an *unsigned* card rather than one signed with
    a published key, because a signature anybody can forge is worse than none: it
    invites a consumer to trust it.
    """
    configured = settings.a2a_signing_key
    if configured is not None:
        return configured.get_secret_value()
    return SYNTHETIC_SIGNING_KEY if settings.mode is RunMode.MOCK else ""


def card_for(settings: Settings) -> AgentCard:
    """Build this deployment's card."""
    key = signing_key(settings)
    return build_card(url=settings.a2a_url, version=__version__, key=key or None)


def build_router(settings: Settings, store: TaskStore | None = None) -> APIRouter:
    """The A2A routes, bound to one task store.

    The store is injectable so a test can drive the lifecycle in-process with no
    network at all, which is how CI exercises this.
    """
    router = APIRouter(tags=["a2a"])
    # Closed over rather than injected through `Depends`: the store is already bound
    # here, and a second injection seam would be a second place a test could end up
    # driving a different store from the one the routes use.
    tasks: TaskStore = store or TaskStore(settings)

    @router.get(CARD_PATH, response_model=AgentCard)
    def agent_card() -> AgentCard:
        """Publish the Agent Card. Read-only, and it says so on the card."""
        return card_for(settings)

    @router.post("/a2a/tasks", response_model=Task, status_code=status.HTTP_201_CREATED)
    def submit(request: SubmitRequest) -> Task:
        """Run one advertised skill and return the finished task."""
        try:
            return tasks.submit(request.skill, {"client": request.client})
        except UnknownSkillError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    @router.get("/a2a/tasks/{task_id}", response_model=Task)
    def task_status(task_id: str) -> Task:
        """Where one task has got to."""
        try:
            return tasks.status(task_id)
        except UnknownTaskError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    @router.get("/a2a/tasks/{task_id}/artifact", response_model=Artifact)
    def task_artifact(task_id: str) -> Artifact:
        """What one completed task produced."""
        try:
            return tasks.artifact(task_id)
        except UnknownTaskError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except TaskError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return router


def mount(app: FastAPI, settings: Settings, store: TaskStore | None = None) -> bool:
    """Attach the A2A routes if this deployment enables them.

    Returns:
        Whether they were attached, so the caller can log it rather than infer it. An
        operator who set the flag and got nothing should be able to tell from one line
        whether the surface is up.
    """
    if not settings.a2a_enabled:
        _log.info("a2a surface disabled; the agent card is published, the endpoint is not")
        return False

    app.include_router(build_router(settings, store))
    _log.info(
        "a2a surface enabled",
        extra={"url": settings.a2a_url, "signed": bool(signing_key(settings))},
    )
    return True


def card_document(settings: Settings) -> dict[str, Any]:
    """The card as a plain dict, for a CLI to print or a file to hold."""
    return card_for(settings).model_dump(mode="json")
