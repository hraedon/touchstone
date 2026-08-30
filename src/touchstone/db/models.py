"""Schema for touchstone.

Governed by ``docs/measurement-model.md``. Read it before changing anything here.

Two structural properties are load-bearing and are expressed in the schema rather
than in prose, because prose does not survive a refactor:

1. **No table holds an eBay user identifier.** Seller usernames are dropped in the
   API client before they reach this layer. Data never stored needs no deletion, no
   register of deletion requests, and cannot be resurrected from a backup — which is
   why the deletion endpoint has nothing to erase. Do not add a seller column.

2. ``ScanAggregate`` has no identifier columns and **no foreign key to Listing**. It
   is written once at scan time and never recomputed, so honoring a deletion cannot
   retroactively rewrite history. There is deliberately no recompute path.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Closed sets. Every dispatch over one of these gets an assert_never default.
# ---------------------------------------------------------------------------


class ScanStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED_BUDGET = "skipped_budget"


class BuyingOption(enum.StrEnum):
    FIXED_PRICE = "FIXED_PRICE"
    AUCTION = "AUCTION"
    BEST_OFFER = "BEST_OFFER"
    CLASSIFIED_AD = "CLASSIFIED_AD"
    OTHER = "OTHER"


class ExtractionMethod(enum.StrEnum):
    REGEX = "regex"
    LLM = "llm"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# Tracked searches and their executions
# ---------------------------------------------------------------------------


class Query(Base):
    """A user-defined eBay search that touchstone samples on a cadence."""

    __tablename__ = "query"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    q: Mapped[str] = mapped_column(String(500))
    category_ids: Mapped[str | None] = mapped_column(String(200))
    # Raw eBay Browse `filter` expression, passed through untouched.
    filter_expr: Mapped[str | None] = mapped_column(String(1000))
    marketplace_id: Mapped[str] = mapped_column(String(20), default="EBAY_US")

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cadence_minutes: Mapped[int] = mapped_column(Integer, default=60)
    # Set by the UI to request an out-of-cadence scan; cleared when honored.
    scan_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # How deep to page. Each page is one API call against a 5,000/day budget.
    max_pages: Mapped[int] = mapped_column(Integer, default=5)

    # Skip listings from sellers below this feedback score. Default 1 excludes
    # zero-feedback accounts, which is where the obvious scam listings cluster.
    # This changes which population is being measured, so the value actually used
    # is recorded on every Scan — see Scan.min_seller_feedback.
    min_seller_feedback: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    scans: Mapped[list[Scan]] = relationship(back_populates="query")


class Scan(Base):
    """One execution of one Query."""

    __tablename__ = "scan"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("query.id", ondelete="CASCADE"))

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ScanStatus] = mapped_column(
        Enum(ScanStatus, native_enum=False, length=32), default=ScanStatus.RUNNING
    )

    api_calls: Mapped[int] = mapped_column(Integer, default=0)
    result_count: Mapped[int] = mapped_column(Integer, default=0)

    # The seller-feedback floor actually applied, recorded per scan rather than
    # read from the query at display time. Raising or lowering it changes the
    # population being sampled, which makes the series discontinuous — and a
    # discontinuity you cannot see is indistinguishable from a market move.
    min_seller_feedback: Mapped[int] = mapped_column(Integer, default=0)
    # How many listings that floor removed. A filter that silently eats most of a
    # result set is otherwise invisible: the numbers just look quieter.
    excluded_low_feedback: Mapped[int] = mapped_column(Integer, default=0)
    # eBay caps a result set at 10,000. A capped scan's "disappearances" are an
    # artifact of the window moving, not market events, so diffing is skipped.
    capped: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)

    query: Mapped[Query] = relationship(back_populates="scans")

    __table_args__ = (Index("ix_scan_query_started", "query_id", "started_at"),)


# ---------------------------------------------------------------------------
# Listings — THE ONLY TABLE HOLDING PERSONAL DATA
# ---------------------------------------------------------------------------


class Listing(Base):
    """A stable eBay item: a product offer, with no attribution to a person.

    There is deliberately no seller column. A listing stripped of its seller is
    market data about an offer, not personal data about a user — the same reasoning
    that lets ``ScanAggregate`` survive independently, applied one level down.

    The consequence is that an account-deletion notification has nothing to match
    and nothing to erase, which is a far stronger position than matching correctly
    would have been: no purge list of deleted users to retain, no replay after a
    restore, and no divergence between eBay's identifier spaces to fall through.
    """

    __tablename__ = "listing"

    # eBay's item id, e.g. "v1|123456789012|0". Stable across scans.
    item_id: Mapped[str] = mapped_column(String(100), primary_key=True)

    title: Mapped[str] = mapped_column(String(500))
    # Hash of the normalized title; joins to ItemSpec so extraction is cached
    # per distinct title rather than per listing.
    title_hash: Mapped[str] = mapped_column(String(64), index=True)

    condition: Mapped[str | None] = mapped_column(String(100))
    # eBay's stable numeric condition code (1000=New, 3000=Used, ...). Preferred
    # over the display string for cohorting: the codes do not drift.
    condition_id: Mapped[str | None] = mapped_column(String(20), index=True)

    buying_option: Mapped[BuyingOption] = mapped_column(
        Enum(BuyingOption, native_enum=False, length=32), default=BuyingOption.OTHER
    )
    item_web_url: Mapped[str | None] = mapped_column(String(1000))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    category_id: Mapped[str | None] = mapped_column(String(50))

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    observations: Mapped[list[ListingObservation]] = relationship(
        back_populates="listing", cascade="all, delete-orphan", passive_deletes=True
    )


class ListingObservation(Base):
    """One listing as seen in one scan. The fact table."""

    __tablename__ = "listing_observation"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(
        ForeignKey("listing.item_id", ondelete="CASCADE"), index=True
    )
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan.id", ondelete="CASCADE"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    price: Mapped[float] = mapped_column(Numeric(12, 2))
    shipping_cost: Mapped[float | None] = mapped_column(Numeric(12, 2))
    total_cost: Mapped[float] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")

    listing: Mapped[Listing] = relationship(back_populates="observations")

    __table_args__ = (
        # A scan sees a listing once.
        UniqueConstraint("listing_id", "scan_id", name="uq_observation_listing_scan"),
    )


class ListingDisappearance(Base):
    """A listing present in one scan and absent from the next.

    This is a *disappearance* series, not a sold series. A listing leaves the active
    pool when it sells, when the seller ends or revises it, or when it expires. We
    cannot distinguish those, and there is deliberately no ``sold_price`` column.
    """

    __tablename__ = "listing_disappearance"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Not an FK: the disappearance is a market observation that must survive the
    # purge of the listing it refers to. Stores no identifiers.
    listing_item_id: Mapped[str] = mapped_column(String(100), index=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("query.id", ondelete="CASCADE"))
    cohort_key: Mapped[str] = mapped_column(String(300), index=True)

    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_price: Mapped[float] = mapped_column(Numeric(12, 2))
    last_total_cost: Mapped[float] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")


# ---------------------------------------------------------------------------
# Aggregates — written once, never recomputed, no identifiers, no listing FK
# ---------------------------------------------------------------------------


class ScanAggregate(Base):
    """Per-cohort statistics, materialized at scan time.

    Deliberately decoupled from Listing. Deleting a seller's listings must not
    change a single value here — that is what keeps a historical chart honest after
    a deletion. Do not add a foreign key, and do not write a recompute function.

    ``n`` is stored even when small; aggregates with n < MIN_COHORT_N are suppressed
    at display time, because an aggregate over one listing is that seller's price in
    a disguise.
    """

    __tablename__ = "scan_aggregate"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan.id", ondelete="CASCADE"), index=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("query.id", ondelete="CASCADE"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cohort_key: Mapped[str] = mapped_column(String(300), index=True)

    n: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")

    price_min: Mapped[float] = mapped_column(Numeric(12, 2))
    price_p10: Mapped[float] = mapped_column(Numeric(12, 2))
    price_p25: Mapped[float] = mapped_column(Numeric(12, 2))
    price_median: Mapped[float] = mapped_column(Numeric(12, 2))
    price_mean: Mapped[float] = mapped_column(Numeric(12, 2))

    # Null until Plan 002 supplies specs; a cohort with no capacity has no $/GB.
    per_gb_min: Mapped[float | None] = mapped_column(Numeric(12, 4))
    per_gb_p10: Mapped[float | None] = mapped_column(Numeric(12, 4))
    per_gb_p25: Mapped[float | None] = mapped_column(Numeric(12, 4))
    per_gb_median: Mapped[float | None] = mapped_column(Numeric(12, 4))
    per_gb_mean: Mapped[float | None] = mapped_column(Numeric(12, 4))

    __table_args__ = (
        UniqueConstraint("scan_id", "cohort_key", name="uq_aggregate_scan_cohort"),
        Index("ix_aggregate_cohort_time", "cohort_key", "observed_at"),
    )


# ---------------------------------------------------------------------------
# Declared now, populated in Plan 002 — so the cohort shape is settled early
# ---------------------------------------------------------------------------


class ItemSpec(Base):
    """Structured attributes extracted from a listing title.

    Keyed by normalized-title hash, not by listing: extraction cost is bounded to
    distinct titles. ``method`` and ``confidence`` are always recorded, and a MANUAL
    correction supersedes a model result permanently.
    """

    __tablename__ = "item_spec"

    title_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    normalized_title: Mapped[str] = mapped_column(String(500))

    capacity_per_module_gb: Mapped[int | None] = mapped_column(Integer)
    module_count: Mapped[int | None] = mapped_column(Integer)
    total_gb: Mapped[int | None] = mapped_column(Integer)
    ddr_gen: Mapped[str | None] = mapped_column(String(10))
    speed_mt: Mapped[int | None] = mapped_column(Integer)
    form_factor: Mapped[str | None] = mapped_column(String(20))
    rank_org: Mapped[str | None] = mapped_column(String(20))
    ecc: Mapped[bool | None] = mapped_column(Boolean)
    registered: Mapped[bool | None] = mapped_column(Boolean)

    method: Mapped[ExtractionMethod | None] = mapped_column(
        Enum(ExtractionMethod, native_enum=False, length=32)
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    model_id: Mapped[str | None] = mapped_column(String(100))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    corrected_by: Mapped[str | None] = mapped_column(String(100))


class Deal(Base):
    """A listing flagged below the p10 of its cohort. Flagged once, not re-alerted."""

    __tablename__ = "deal"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(
        ForeignKey("listing.item_id", ondelete="CASCADE"), index=True
    )
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan.id", ondelete="CASCADE"))
    cohort_key: Mapped[str] = mapped_column(String(300), index=True)

    total_cost: Mapped[float] = mapped_column(Numeric(12, 2))
    per_gb: Mapped[float | None] = mapped_column(Numeric(12, 4))
    cohort_p10: Mapped[float] = mapped_column(Numeric(12, 4))
    cohort_n: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Numeric(6, 3))

    flagged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("listing_id", name="uq_deal_listing"),)


class Watch(Base):
    """A listing pinned by the operator, tracked regardless of sampling."""

    __tablename__ = "watch"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(
        ForeignKey("listing.item_id", ondelete="CASCADE"), unique=True
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------


class RateBudget(Base):
    """Local ledger of API spend, per UTC day.

    A fallback, not the source of truth: the scheduler reads remaining quota from
    eBay's getRateLimits and only falls back here when that call fails. It never
    falls back to "unlimited" — a counter that drifts silently is a check whose
    failure mode is silence.
    """

    __tablename__ = "rate_budget"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD, UTC
    calls_used: Mapped[int] = mapped_column(Integer, default=0)
    calls_limit: Mapped[int] = mapped_column(Integer, default=5000)
    last_authoritative_read: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_authoritative_remaining: Mapped[int | None] = mapped_column(Integer)


class DeletionReceipt(Base):
    """Proof that every account-deletion notification was received and answered.

    **Stores no identifiers.** An earlier design kept the notification's username,
    userId, and eiasToken so a purge could match on them — which amounted to
    maintaining a permanent register of the people who had asked to be forgotten, in
    order to demonstrate that we had forgotten them. Since no seller data is stored
    in the first place, none of that is needed: the notification id and a timestamp
    are enough to show the endpoint answered.

    The compliance claim this supports is stronger than an audit log, because it is
    checkable from the schema rather than from records we wrote about ourselves:
    there is no seller column anywhere for a deletion to have missed.
    """

    __tablename__ = "deletion_receipt"

    notification_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
