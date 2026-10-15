"""Typed requests and responses for the Azure tool surface.

Two properties are built into these types rather than left to the handlers.

**No write operation can be expressed.** :class:`ToolOperation` has exactly one
member. There is no value a caller could pass to declare a mutating tool, so a write
tool cannot be registered -- it is absent rather than disabled. A boolean
``read_only=True`` flag would have been a convention; a single-member enum is a
constraint.

**A failure cannot look like an empty success.** Every response carries a
:class:`ToolStatus` of ``verified``, ``unknown`` or ``failed``, and a non-verified
response must say why. A tool that could not reach Azure returns ``failed`` with the
reason, never an empty list -- because an empty list of resources and a failed
inventory query mean very different things to a cost report, and conflating them is
how a tool quietly tells a client their estate is clean.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolOperation(StrEnum):
    """What a tool is permitted to do.

    Deliberately a single member. CostSentinel's Azure surface is read-only, and the
    cheapest way to guarantee that is to make the alternative unrepresentable. If a
    later phase ever needs a write path it will have to add a member here, which is a
    visible, reviewable act rather than a flag flipped in a call.
    """

    READ = "read"


class ToolStatus(StrEnum):
    """How much to trust a tool response.

    ``UNKNOWN`` is not a soft failure. It means the call succeeded and Azure does not
    know -- a VM with no metrics because it has never been started, say. That is a
    fact worth carrying, and it is the difference between "nothing is using this" and
    "nobody measured".
    """

    VERIFIED = "verified"
    UNKNOWN = "unknown"
    FAILED = "failed"

    @property
    def is_usable(self) -> bool:
        """Whether the payload carries data a caller may rely on."""
        return self is ToolStatus.VERIFIED


class ToolModel(BaseModel):
    """Base for everything crossing the tool boundary.

    ``extra="forbid"`` means an unexpected field from an Azure payload is a
    validation error rather than silently-carried data, which matters because these
    responses are built from JSON that Azure, not CostSentinel, controls.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolResponse(ToolModel):
    """Common envelope for every tool response."""

    status: ToolStatus
    reason: str = Field(
        default="",
        description="Why the status is not verified. Mandatory unless it is.",
    )
    api_version: str = ""
    retrieved_at: datetime | None = None

    @model_validator(mode="after")
    def _require_a_reason_when_not_verified(self) -> Self:
        if self.status is not ToolStatus.VERIFIED and not self.reason.strip():
            msg = (
                f"a {self.status.value} response must say why; an unexplained "
                f"non-verified result is indistinguishable from a bug"
            )
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class ListSubscriptionsRequest(ToolModel):
    """List the subscriptions the caller's credential can see."""

    tenant_id: str | None = None


class SubscriptionRecord(ToolModel):
    """One subscription, as Resource Graph reports it."""

    subscription_id: str
    display_name: str
    state: str = ""
    tenant_id: str = ""
    tags: dict[str, str] = Field(default_factory=dict)


class ListSubscriptionsResponse(ToolResponse):
    """Subscriptions in scope."""

    subscriptions: tuple[SubscriptionRecord, ...] = ()

    @model_validator(mode="after")
    def _failed_means_empty(self) -> Self:
        if self.status is ToolStatus.FAILED and self.subscriptions:
            msg = "a failed response must not carry data"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Inventory (Resource Graph)
# ---------------------------------------------------------------------------


class QueryResourcesRequest(ToolModel):
    """Inventory for one subscription.

    The subscription is required. A real run targets what the operator named rather
    than everything the credential can reach.
    """

    subscription_id: str = Field(min_length=1)
    resource_types: tuple[str, ...] = ()
    page_size: int = Field(default=500, ge=1, le=1000)


class ResourceRecord(ToolModel):
    """One resource, as Resource Graph reports it."""

    resource_id: str
    name: str
    resource_type: str
    location: str
    resource_group: str = ""
    subscription_id: str = ""
    sku_name: str | None = None
    sku_tier: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Flattened scalar properties only; nested objects are dropped.",
    )
    created_time: datetime | None = None
    disk_size_gb: Decimal | None = Field(default=None, gt=Decimal(0))
    managed_by: str | None = Field(
        default=None,
        description="What this resource is attached to, if anything. The orphan test.",
    )


class QueryResourcesResponse(ToolResponse):
    """Inventory records."""

    resources: tuple[ResourceRecord, ...] = ()
    truncated: bool = Field(
        default=False,
        description="True when more pages exist than were fetched, so a caller knows "
        "the inventory is partial rather than complete.",
    )

    @model_validator(mode="after")
    def _failed_means_empty(self) -> Self:
        if self.status is ToolStatus.FAILED and self.resources:
            msg = "a failed response must not carry data"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


class ListReservationsRequest(ToolModel):
    """Capacity commitments visible to the caller."""

    subscription_id: str | None = None


class ReservationRecord(ToolModel):
    """One reservation, as the Reservations API reports it."""

    reservation_id: str
    name: str
    reserved_sku: str
    reserved_type: str = ""
    region: str = ""
    scope_subscription_id: str | None = None
    term_months: int = Field(ge=1)
    quantity: int = Field(ge=1)
    utilisation_pct: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    expires_on: date | None = None


class ListReservationsResponse(ToolResponse):
    """Reservation records."""

    reservations: tuple[ReservationRecord, ...] = ()

    @model_validator(mode="after")
    def _failed_means_empty(self) -> Self:
        if self.status is ToolStatus.FAILED and self.reservations:
            msg = "a failed response must not carry data"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Cost Management
# ---------------------------------------------------------------------------


class GetCostSeriesRequest(ToolModel):
    """Daily actual spend for one subscription over a trailing window."""

    subscription_id: str = Field(min_length=1)
    window_days: int = Field(default=30, ge=1, le=365)


class CostPointRecord(ToolModel):
    """One day of actual spend."""

    day: date
    amount: Decimal = Field(ge=Decimal(0))


class GetCostSeriesResponse(ToolResponse):
    """A daily cost series."""

    subscription_id: str = ""
    currency: str = ""
    points: tuple[CostPointRecord, ...] = ()

    @model_validator(mode="after")
    def _failed_means_empty(self) -> Self:
        if self.status is ToolStatus.FAILED and self.points:
            msg = "a failed response must not carry data"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Monitor metrics
# ---------------------------------------------------------------------------


class GetResourceMetricsRequest(ToolModel):
    """Observed utilisation for one resource."""

    resource_id: str = Field(min_length=1)
    window_days: int = Field(default=30, ge=1, le=90)


class GetResourceMetricsResponse(ToolResponse):
    """Utilisation, with every field optional.

    A metric Azure did not return is ``None``, and the status is ``UNKNOWN`` when
    none came back at all. The detectors treat an absent metric as "cannot conclude"
    rather than as zero, which is the whole reason these are optional.
    """

    resource_id: str = ""
    observation_days: int = Field(default=0, ge=0)
    cpu_avg_pct: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    cpu_max_pct: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    network_in_gb: Decimal | None = Field(default=None, ge=Decimal(0))
    connection_count: int | None = Field(default=None, ge=0)
    utilisation_pct: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(100))

    @property
    def has_any_metric(self) -> bool:
        """Whether Azure returned anything at all."""
        return any(
            value is not None
            for value in (
                self.cpu_avg_pct,
                self.cpu_max_pct,
                self.network_in_gb,
                self.connection_count,
                self.utilisation_pct,
            )
        )


# ---------------------------------------------------------------------------
# Advisor
# ---------------------------------------------------------------------------


class ListAdvisorRecommendationsRequest(ToolModel):
    """Azure Advisor's own cost recommendations for one subscription."""

    subscription_id: str = Field(min_length=1)


class AdvisorRecommendationRecord(ToolModel):
    """One Advisor cost recommendation.

    Carried as corroboration, never as a finding. CostSentinel's own detectors decide
    what is wasteful; Advisor agreeing is evidence, and Advisor is not a substitute
    for a deterministic rule whose precision we can measure.
    """

    recommendation_id: str
    category: str = ""
    impact: str = ""
    problem: str = ""
    solution: str = ""
    resource_id: str | None = None
    annual_saving_amount: Decimal | None = Field(default=None, ge=Decimal(0))
    annual_saving_currency: str = ""


class ListAdvisorRecommendationsResponse(ToolResponse):
    """Advisor records."""

    recommendations: tuple[AdvisorRecommendationRecord, ...] = ()

    @model_validator(mode="after")
    def _failed_means_empty(self) -> Self:
        if self.status is ToolStatus.FAILED and self.recommendations:
            msg = "a failed response must not carry data"
            raise ValueError(msg)
        return self


def failure(response_type: type[ToolResponse], reason: str) -> ToolResponse:
    """Build a failed response of the given type.

    A helper rather than a pattern, so no handler has to remember that a failure
    carries a reason and no data.
    """
    return response_type(status=ToolStatus.FAILED, reason=reason)
