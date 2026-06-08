"""Common interface every Open Laws Foundation jurisdiction adapter implements.

The whole point of the project lives here: an adapter is *only* required to
answer three questions about its jurisdiction — what changed, how to fetch it,
how to express its identity/time/citations — and the runner + conformance suite
do the rest identically across countries.

An adapter never invents a content schema. It emits native Akoma Ntoso (in the
jurisdiction's own AKN profile) plus the normalized AKN4OLF metadata layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator, Protocol


@dataclass(frozen=True)
class ActRef:
    """A pointer to one act in the source system, before fetching.

    Identity is CANONICAL-FROM-AKN: ``olf_id`` and ``native_urn`` are not known at
    discovery time and are derived from the fetched Akoma Ntoso during transform
    (the document carries its own self-id in ``<FRBRWork>``). At discovery we only
    know the source *coordinates* needed to fetch the act and to match the right
    entry inside the export ZIP — ``denominazione`` / ``anno`` / ``numero`` /
    ``codice_redazionale`` / ``data_gu``. Discovery therefore yields an ActRef with
    the coordinates set and ``olf_id``/``native_urn`` left ``None``; transform fills
    in the derived canonical identity.
    """

    # Canonical identity — DERIVED from the AKN <FRBRWork> during transform, not
    # known at discovery. Optional everywhere upstream of transform.
    olf_id: str | None = None        # e.g. "olf:it/legge/2019/123"
    native_urn: str | None = None    # e.g. "urn:nir:stato:legge:2019-08-05;123"
    source_modified: datetime | None = None  # last change seen at the source, if known
    # Source coordinates used only to FETCH and to MATCH the right act/version.
    denominazione: str | None = None      # search label, e.g. "DECRETO-LEGGE"
    anno: str | None = None               # provvedimento year, e.g. "2024"
    numero: str | None = None             # provvedimento number, e.g. "19"
    codice_redazionale: str | None = None  # e.g. "24G00035" (embedded in ZIP names)
    data_gu: str | None = None            # Gazzetta Ufficiale date, e.g. "2024-03-02"


@dataclass
class SourceDocument:
    """Raw material exactly as fetched from the official source."""

    ref: ActRef
    raw_bytes: bytes              # the source payload (Akoma Ntoso XML for IT)
    media_type: str               # e.g. "application/akn+xml"
    source_url: str               # exact URL it came from (provenance)
    fetched_at: datetime


@dataclass
class Provenance:
    """Recorded into every produced document. This is what makes the archive a
    reproducible cache rather than a hand-curated corpus."""

    source_url: str
    source_sha256: str            # hash of raw_bytes
    adapter_name: str
    adapter_version: str
    generated_at: datetime


@dataclass
class OLFDocument:
    """The adapter's output: native AKN with the AKN4OLF metadata layer applied."""

    ref: ActRef
    akn_xml: bytes                # serialized Akoma Ntoso, AKN4OLF-conformant
    provenance: Provenance
    # convenience extraction for the runner / archive layout; the source of truth
    # is always akn_xml, these are derived.
    in_force_from: date | None = None
    in_force_to: date | None = None
    references: list[str] = field(default_factory=list)  # target OLF ids


class Adapter(Protocol):
    """Implement this for a jurisdiction. See adapters/it for the reference impl."""

    name: str           # "normattiva-it"
    version: str        # semver of THIS adapter; bumps force regeneration
    jurisdiction: str   # "it"

    def discover(self, since: datetime | None) -> Iterator[ActRef]:
        """Yield acts changed at the source since `since` (None = full backfill).

        Incremental runs pass a timestamp; backfill passes None. The adapter is
        responsible for translating that into source-specific queries.
        """
        ...

    def fetch(self, ref: ActRef) -> SourceDocument:
        """Retrieve one act's raw payload from the official source."""
        ...

    def transform(self, doc: SourceDocument) -> OLFDocument:
        """Validate + attach the AKN4OLF layer. Must NOT alter substantive text.

        Raises ConformanceError if the result would not pass the suite, so a bad
        document never reaches the archive.
        """
        ...


class ConformanceError(Exception):
    """Raised when a produced document would fail the AKN4OLF conformance suite."""
