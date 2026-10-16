"""Labelled estates: the estate specification, and the right answers.

A dataset is one committed JSON file holding both halves — the estate and what the
correct output is — so a reviewer sees them side by side. Splitting them across two
files makes it easy for one to drift from the other, and a label that no longer
matches its estate is worse than no label.

**Ground truth is committed, not recomputed.** Each wasteful resource carries the
exact monthly saving the pipeline should report, as a decimal string a human reviewed
once. The tempting alternative is to compute it at scoring time from the price
catalogue, which would make the cost-figure gate compare a calculation to itself and
pass unconditionally. The cost of committing it is that refreshing the catalogue
breaks these files loudly — which is the same bargain the savings assertions in
``tests/test_graph.py`` already make, and the right way round.

The estate is a *specification* rather than a snapshot of domain objects: resources
name their SKU, state, tags and measured utilisation, and
:class:`~evals.harness.provider.DatasetProvider` prices them from the committed
catalogue exactly as the mock provider does. That keeps the dataset readable and
keeps one pricing path in the system.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, ValidationError, model_validator

from costsentinel.domain.common import Frozen
from costsentinel.domain.estate import Environment, ResourceKind, ResourceState
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.signals import WasteKind


class DatasetError(RuntimeError):
    """A dataset could not be loaded or does not describe a coherent estate."""


class Tier(StrEnum):
    """Whether a dataset runs on every commit or only on demand.

    ``SMOKE`` datasets run in the normal CI job and must stay fast. At least one
    adversarial case belongs in the smoke set: a smoke run over easy cases only would
    look like coverage while measuring nothing.
    """

    SMOKE = "smoke"
    FULL = "full"


class AdversarialKind(StrEnum):
    """Why a case is hard, when it is deliberately hard.

    An eval over easy cases measures nothing except that the easy cases work. These
    are the three ways a cost tool is actually wrong in the field.
    """

    LOOKS_WASTEFUL_IS_HEALTHY = "looks_wasteful_is_healthy"
    LOOKS_HEALTHY_IS_WASTEFUL = "looks_healthy_is_wasteful"
    PROMPT_INJECTION = "prompt_injection"


# ---------------------------------------------------------------------------
# The estate specification
# ---------------------------------------------------------------------------


class SubscriptionSpec(Frozen):
    """One subscription in a dataset estate."""

    subscription_id: str = Field(pattern=r"^00000000-0000-4000-8000-[0-9a-f]{12}$")
    display_name: str
    environment: Environment = Environment.UNKNOWN
    region: str


class ResourceSpec(Frozen):
    """One resource, with whatever was measured about it.

    No cost field. The price comes from the catalogue by ``(sku, kind, region)``,
    which is the same path the mock provider and the real provider use — so a dataset
    cannot accidentally assert a price that Azure does not charge.
    """

    name: str
    kind: ResourceKind
    subscription: int = Field(ge=0, description="Index into the estate's subscriptions.")
    resource_group: str
    state: ResourceState
    sku: str | None = None
    attached_to: str | None = Field(
        default=None, description="Name of the resource this one is attached to, if any."
    )
    attached_to_kind: ResourceKind = ResourceKind.VIRTUAL_MACHINE
    tags: dict[str, str] = Field(default_factory=dict)
    age_days: int = Field(default=0, ge=0)
    capacity_gb: Decimal | None = Field(default=None, gt=Decimal(0))

    # Measured utilisation. All optional: absent means not measured, which the
    # detectors must read as "cannot conclude" rather than as idle.
    cpu_avg_pct: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    cpu_max_pct: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    network_in_gb: Decimal | None = Field(default=None, ge=Decimal(0))
    connection_count: int | None = Field(default=None, ge=0)
    utilisation_pct: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(100))

    @property
    def has_metrics(self) -> bool:
        """Whether anything was measured about this resource."""
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


class ReservationSpec(Frozen):
    """One capacity commitment in a dataset estate."""

    name: str
    subscription: int | None = Field(
        default=None, description="None for a shared-scope reservation."
    )
    reserved_sku: str
    reserved_kind: ResourceKind = ResourceKind.VIRTUAL_MACHINE
    term_months: int = Field(ge=1)
    quantity: int = Field(ge=1)
    utilisation_pct: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    expires_in_days: int = Field(default=365)


class EstateSpec(Frozen):
    """The estate a dataset presents to the pipeline."""

    client: str
    daily_spend: Decimal = Field(
        gt=Decimal(0),
        description=(
            "Flat daily actual spend per subscription. Flat rather than varied "
            "because a cost series with noise in it would make the observed-spend "
            "figure depend on a random seed, and this phase scores figures exactly."
        ),
    )
    subscriptions: tuple[SubscriptionSpec, ...] = Field(min_length=1)
    resources: tuple[ResourceSpec, ...] = Field(min_length=1)
    reservations: tuple[ReservationSpec, ...] = ()

    @model_validator(mode="after")
    def _subscription_indices_exist(self) -> Self:
        count = len(self.subscriptions)
        for resource in self.resources:
            if resource.subscription >= count:
                msg = (
                    f"resource {resource.name!r} names subscription "
                    f"{resource.subscription}, but the estate has {count}"
                )
                raise ValueError(msg)
        for reservation in self.reservations:
            if reservation.subscription is not None and reservation.subscription >= count:
                msg = (
                    f"reservation {reservation.name!r} names subscription "
                    f"{reservation.subscription}, but the estate has {count}"
                )
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# The labels
# ---------------------------------------------------------------------------


class ResourceLabel(Frozen):
    """The right answer for one resource.

    Healthy resources are labelled too, and not as an afterthought: an estate of
    nothing but waste cannot measure precision, because there is nothing a detector
    could wrongly flag.
    """

    resource: str = Field(
        description=(
            "The name of the resource or reservation this label is about. Unique within a dataset."
        )
    )
    is_wasteful: bool
    waste_kind: WasteKind | None = None
    expected_action: ActionType | None = None
    expected_action_class: ActionClass | None = Field(
        default=None,
        description=(
            "The weakest class this action may be given. The Policy Guard may "
            "classify it more strictly -- a floor may be tightened, never loosened "
            "(D8) -- so safety recall checks 'at least as strict as' rather than "
            "equality."
        ),
    )
    expected_monthly_saving: Decimal | None = Field(
        default=None,
        ge=Decimal(0),
        description=(
            "Exact figure the pipeline must report. Committed rather than recomputed, "
            "so the cost-figure gate is not comparing a calculation to itself."
        ),
    )
    expected_rank: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Where a FinOps reviewer would place this finding. A hand-written "
            "judgement, not a derived figure -- which is the point: ranking quality "
            "is measured against what a person would do, not against a restatement "
            "of the weights the ranker already uses."
        ),
    )
    adversarial: AdversarialKind | None = None
    note: str = Field(
        min_length=1,
        description=(
            "Why this is the right answer. Mandatory, so a future disagreement is "
            "with a stated argument rather than with a bare boolean."
        ),
    )

    @model_validator(mode="after")
    def _wasteful_labels_are_complete(self) -> Self:
        if self.is_wasteful:
            missing = [
                name
                for name, value in (
                    ("waste_kind", self.waste_kind),
                    ("expected_action", self.expected_action),
                    ("expected_action_class", self.expected_action_class),
                )
                if value is None
            ]
            if missing:
                msg = (
                    f"{self.resource!r} is labelled wasteful but does not say "
                    f"{', '.join(missing)}. A half-labelled positive case scores "
                    f"nothing and silently weakens recall."
                )
                raise ValueError(msg)
        elif self.waste_kind is not None or self.expected_action is not None:
            msg = (
                f"{self.resource!r} is labelled healthy but carries a waste kind or "
                f"an expected action. One of the two is wrong."
            )
            raise ValueError(msg)
        return self


class LabelledEstate(Frozen):
    """One dataset: an estate, and what the pipeline should say about it."""

    dataset_id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str
    purpose: str = Field(
        min_length=1, description="What this dataset is for, in one or two sentences."
    )
    tier: Tier = Tier.FULL
    estate: EstateSpec
    labels: tuple[ResourceLabel, ...] = Field(min_length=1)
    injection_control: str | None = Field(
        default=None,
        description=(
            "The dataset id this one is the hostile twin of. Injection resistance is "
            "measured differentially: the same estate with benign names and with "
            "hostile ones must produce identical actions, classes and figures."
        ),
    )

    @property
    def labellable(self) -> frozenset[str]:
        """Everything a label may be about.

        Reservations are included because the detectors produce findings for them,
        so leaving them unlabellable would mean a real finding was neither a true
        positive nor a false one -- it would simply not be counted, which is the
        worst of the three.
        """
        return frozenset(
            {resource.name for resource in self.estate.resources}
            | {reservation.name for reservation in self.estate.reservations}
        )

    @model_validator(mode="after")
    def _labels_match_the_estate(self) -> Self:
        resources = self.labellable
        labelled = {label.resource for label in self.labels}

        unknown = labelled - resources
        if unknown:
            msg = (
                f"labels name resources or reservations not in the estate: "
                f"{', '.join(sorted(unknown))}"
            )
            raise ValueError(msg)

        # Every resource must be labelled, including the healthy ones. An unlabelled
        # resource is indistinguishable from one nobody got round to deciding about,
        # and would quietly be excluded from precision.
        unlabelled = resources - labelled
        if unlabelled:
            msg = (
                f"every resource and reservation needs a label, including the "
                f"healthy ones. Missing: {', '.join(sorted(unlabelled))}"
            )
            raise ValueError(msg)

        if len(labelled) != len(self.labels):
            msg = "two labels name the same resource"
            raise ValueError(msg)
        return self

    # --- lookups ---------------------------------------------------------

    def label_for(self, resource_name: str) -> ResourceLabel | None:
        """One label by resource name."""
        return next((label for label in self.labels if label.resource == resource_name), None)

    @property
    def wasteful(self) -> tuple[ResourceLabel, ...]:
        """Labels for the resources that genuinely are waste."""
        return tuple(label for label in self.labels if label.is_wasteful)

    @property
    def healthy(self) -> tuple[ResourceLabel, ...]:
        """Labels for the resources a detector must leave alone."""
        return tuple(label for label in self.labels if not label.is_wasteful)

    @property
    def adversarial(self) -> tuple[ResourceLabel, ...]:
        """Labels for the deliberately hard cases."""
        return tuple(label for label in self.labels if label.adversarial is not None)

    @property
    def destructive(self) -> tuple[ResourceLabel, ...]:
        """Labels whose expected remediation needs a human.

        What safety recall is computed over. A label with no expected class is not
        destructive and is not in the denominator.
        """
        return tuple(
            label
            for label in self.labels
            if label.expected_action_class is not None
            and label.expected_action_class is not ActionClass.ALLOW
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def datasets_dir() -> Path:
    """Where the committed datasets live."""
    return Path(__file__).resolve().parent.parent / "datasets"


def load_dataset(path: Path) -> LabelledEstate:
    """Read and validate one dataset.

    Raises:
        DatasetError: If the file is unreadable, malformed, or describes an estate
            its labels do not match. Loudly, because a dataset that half-loaded would
            score a partial estate against partial labels and report a number.
    """
    try:
        return LabelledEstate.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, json.JSONDecodeError, OSError) as exc:
        msg = f"{path.name} is not a usable dataset: {exc}"
        raise DatasetError(msg) from exc


@lru_cache(maxsize=1)
def load_all() -> tuple[LabelledEstate, ...]:
    """Every committed dataset, in a stable order.

    Sorted by id so a scorecard's row order does not depend on the filesystem.
    """
    paths = sorted(datasets_dir().glob("*.json"))
    if not paths:  # pragma: no cover - the datasets are committed
        msg = f"no datasets found in {datasets_dir()}"
        raise DatasetError(msg)
    loaded = tuple(sorted((load_dataset(path) for path in paths), key=lambda d: d.dataset_id))

    ids = [dataset.dataset_id for dataset in loaded]
    if len(set(ids)) != len(ids):
        msg = f"two datasets share an id: {sorted(ids)}"
        raise DatasetError(msg)
    return loaded


def select(tier: Tier | None = None, dataset_id: str | None = None) -> tuple[LabelledEstate, ...]:
    """The datasets a run should cover.

    Args:
        tier: ``SMOKE`` for the fast subset, ``None`` for everything.
        dataset_id: One dataset by id, which overrides ``tier``.

    Raises:
        DatasetError: If a named dataset does not exist, or a tier is empty.
    """
    everything = load_all()
    if dataset_id is not None:
        found = next((d for d in everything if d.dataset_id == dataset_id), None)
        if found is None:
            available = ", ".join(d.dataset_id for d in everything)
            msg = f"no dataset {dataset_id!r}. Available: {available}"
            raise DatasetError(msg)
        return (found,)
    if tier is None:
        return everything
    chosen = tuple(d for d in everything if d.tier is tier)
    if not chosen:
        msg = f"no datasets in the {tier.value} tier"
        raise DatasetError(msg)
    return chosen


def control_for(
    dataset: LabelledEstate, available: Sequence[LabelledEstate]
) -> LabelledEstate | None:
    """The benign twin a hostile dataset is compared against, if it names one."""
    if dataset.injection_control is None:
        return None
    return next((d for d in available if d.dataset_id == dataset.injection_control), None)
