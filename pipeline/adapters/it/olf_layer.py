"""Apply the AKN4OLF metadata layer onto native Normattiva Akoma Ntoso.

This is where "normalize the metadata, not the content" becomes code. We DO NOT
touch the substantive text, structure, or the native AKN profile produced by
Normattiva. We only:

  1. stamp the OLF identity (mapped from the NIR URN),
  2. normalize lifecycle events into the AKN4OLF vocabulary,
  3. type the references,
  4. embed provenance.

Everything else in the document is passed through untouched.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from lxml import etree

from ..base import OLFDocument, Provenance, SourceDocument, ConformanceError
from . import urn as urnlib

AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
OLF_NS = "https://openlawsfoundation.org/ns/akn4olf/1.0"
NSMAP = {"akn": AKN_NS, "olf": OLF_NS}


def build(doc: SourceDocument, *, adapter_name: str, adapter_version: str) -> OLFDocument:
    try:
        tree = etree.fromstring(doc.raw_bytes)
    except etree.XMLSyntaxError as e:
        raise ConformanceError(f"Source is not well-formed XML: {e}") from e

    # 1. identity ------------------------------------------------------
    olf_id = doc.ref.olf_id
    _set_olf_meta(tree, "identity", {
        "olfId": olf_id,
        "nativeUrn": doc.ref.native_urn,
        "jurisdiction": "it",
    })

    # 2. lifecycle events ---------------------------------------------
    # Normattiva carries multivigenza in native AKN temporal elements; we read
    # them and re-express the validity interval in the normalized OLF vocab.
    in_force_from, in_force_to = _extract_validity(tree)
    _set_olf_meta(tree, "lifecycle", {
        "inForceFrom": in_force_from.isoformat() if in_force_from else "",
        "inForceTo": in_force_to.isoformat() if in_force_to else "",
    })

    # 3. references ----------------------------------------------------
    refs = _normalize_refs(tree)

    # 4. provenance ----------------------------------------------------
    prov = Provenance(
        source_url=doc.source_url,
        source_sha256=hashlib.sha256(doc.raw_bytes).hexdigest(),
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        generated_at=datetime.now(timezone.utc),
    )
    _set_olf_meta(tree, "provenance", {
        "sourceUrl": prov.source_url,
        "sourceSha256": prov.source_sha256,
        "adapter": f"{prov.adapter_name}@{prov.adapter_version}",
        "generatedAt": prov.generated_at.isoformat().replace("+00:00", "Z"),
    })

    akn_xml = etree.tostring(tree, xml_declaration=True, encoding="UTF-8")
    return OLFDocument(
        ref=doc.ref,
        akn_xml=akn_xml,
        provenance=prov,
        in_force_from=in_force_from,
        in_force_to=in_force_to,
        references=refs,
    )


# --- helpers ---------------------------------------------------------

OLF_PROPRIETARY_SOURCE = "#openlawsfoundation"


def _olf_block(tree: etree._Element) -> etree._Element:
    """Find or create OLF's OWN <akn:proprietary><olf:meta> block under <meta>.

    Real Normattiva documents already carry a native ``<proprietary source="">``
    holding their RDF descriptor. We must NOT hijack that block. Instead we look
    for (or create) a proprietary element specifically identified as OLF's via
    ``source="#openlawsfoundation"`` and put the OLF metadata there, keeping it
    cleanly separated from the native RDF.
    """
    meta = tree.find(f".//{{{AKN_NS}}}meta")
    if meta is None:
        raise ConformanceError("Document has no <akn:meta>; not valid AKN")
    # Find OLF's own proprietary block (never any other proprietary, e.g. RDF).
    prop = None
    for candidate in meta.findall(f"{{{AKN_NS}}}proprietary"):
        if candidate.get("source") == OLF_PROPRIETARY_SOURCE:
            prop = candidate
            break
    if prop is None:
        prop = etree.SubElement(meta, f"{{{AKN_NS}}}proprietary")
        prop.set("source", OLF_PROPRIETARY_SOURCE)
    olf = prop.find(f"{{{OLF_NS}}}meta")
    if olf is None:
        olf = etree.SubElement(prop, f"{{{OLF_NS}}}meta")
    return olf


def _set_olf_meta(tree: etree._Element, group: str, fields: dict[str, str]) -> None:
    olf = _olf_block(tree)
    el = olf.find(f"{{{OLF_NS}}}{group}")
    if el is None:
        el = etree.SubElement(olf, f"{{{OLF_NS}}}{group}")
    for k, v in fields.items():
        el.set(k, v or "")


def _extract_validity(tree: etree._Element) -> tuple[date | None, date | None]:
    """Resolve the act-level validity interval, real-data-aware and layered.

    Normattiva's exported AKN profile does NOT use the OASIS temporal model
    (``temporalData``/``temporalGroup``/``timeInterval`` are absent in real
    documents). Instead validity is carried by ``<lifecycle>/<eventRef>`` events
    and by ``identification/FRBRExpression/FRBRdate``. The idealized AKN-standard
    documents the conformance suite exercises, however, DO use ``temporalData``.

    To support both, we try a series of strategies and return the first that
    yields a start date:

      a. **Standard AKN temporalData** — the OASIS model
         (``temporalGroup``/``timeInterval`` anchored to ``<eventRef>`` dates).
         Kept verbatim for the synthetic/conformance corpus.
      b. **Lifecycle eventRefs** — the real Normattiva common case: the earliest
         ``<lifecycle>/<eventRef>`` date is the in-force start; a repeal/abrogation
         event (``@type``/``@refersTo`` containing "abrog"/"repeal"/"abol")
         supplies the end, else the act is still in force (``None``).
      c. **FRBRExpression date** — ``identification/FRBRExpression/FRBRdate@date``
         as the in-force start (end ``None``).
      d. Otherwise ``(None, None)``.

    The function never raises; any unparseable boundary degrades to ``None``.

    Returns:
        ``(in_force_from, in_force_to)`` — either value may be ``None``.
    """
    # a. OASIS AKN-standard temporalData (synthetic / conformance corpus).
    start, end = _validity_from_temporal_data(tree)
    if start is not None:
        return start, end

    # b. Lifecycle eventRefs (real Normattiva).
    start, end = _validity_from_lifecycle(tree)
    if start is not None:
        return start, end

    # c. FRBRExpression date.
    start = _validity_from_frbr_expression(tree)
    if start is not None:
        return start, None

    # d. Nothing resolvable.
    return None, None


def _validity_from_temporal_data(
    tree: etree._Element,
) -> tuple[date | None, date | None]:
    """Read the act-level validity interval from native AKN 3.0 temporal data.

    Follows the OASIS Akoma Ntoso 3.0 temporal model (OASIS Standard,
    LegalDocumentML TC, 2018): ``<temporalData>`` holds ``<temporalGroup>``
    elements each describing one validity period via ``<timeInterval>``;
    ``<eventRef>`` elements in ``<lifecycle>`` anchor the period boundaries to
    concrete dates.

    Act-level temporalGroup selection heuristic (in priority order):

    1. If any element in the document carries a ``period`` attribute (e.g.
       ``<act period="#tg1">``), the FIRST such element in document order names
       the act-level group; the leading ``#`` is stripped and matched against
       ``temporalGroup/@eId``.
    2. If there is exactly one ``temporalGroup``, use it directly.
    3. Prefer a group whose first ``<timeInterval>/@refersTo`` (lowercased)
       contains ``"force"`` (EN *inForce*), ``"vigor"`` (IT *inVigore*), or
       ``"vigen"`` (IT *vigente* / *vigenza*).
    4. Fall back to the first ``temporalGroup`` in document order.

    The function degrades gracefully: any missing or malformed boundary yields
    ``None`` for that endpoint.  An open-ended interval (no ``end`` attribute)
    returns ``None`` for ``in_force_to``, signalling the act is still in force.

    Returns:
        ``(in_force_from, in_force_to)`` — either value may be ``None``.
    """
    # Step 1: locate <temporalData>
    temporal_data = tree.find(f".//{{{AKN_NS}}}temporalData")
    if temporal_data is None:
        return None, None

    # Step 2: collect temporalGroup children
    groups = temporal_data.findall(f"{{{AKN_NS}}}temporalGroup")
    if not groups:
        return None, None

    # Step 3: choose the act-level temporalGroup
    chosen_group = None

    # 3a. look for @period on ANY element (first in document order)
    for el in tree.iter():
        period_ref = el.get("period")
        if period_ref is not None:
            group_id = period_ref.lstrip("#")
            for g in groups:
                if g.get("eId") == group_id:
                    chosen_group = g
                    break
            break  # only the first @period-carrying element counts

    if chosen_group is None:
        if len(groups) == 1:
            # 3b. single group — unambiguous
            chosen_group = groups[0]
        else:
            # 3c. prefer a group whose first timeInterval refersTo contains
            #     "force" (inForce), "vigor" (inVigore), or "vigen" (vigente/vigenza)
            for g in groups:
                ti = g.find(f"{{{AKN_NS}}}timeInterval")
                if ti is not None:
                    refers_to = (ti.get("refersTo") or "").lower()
                    if any(stem in refers_to for stem in ("force", "vigor", "vigen")):
                        chosen_group = g
                        break

        if chosen_group is None:
            # 3d. last resort: first group in document order
            chosen_group = groups[0]

    # Step 4: read start/end from the FIRST timeInterval
    time_interval = chosen_group.find(f"{{{AKN_NS}}}timeInterval")
    if time_interval is None:
        return None, None

    start_ref = time_interval.get("start")
    end_ref = time_interval.get("end")

    # Step 5 & 6: resolve refs to dates, never raise
    start_date = _resolve_event_date(tree, start_ref)
    end_date = _resolve_event_date(tree, end_ref)

    return start_date, end_date


# Event-type stems (lowercased substrings) that mark the END of validity.
_REPEAL_STEMS = ("abrog", "repeal", "abol")


def _validity_from_lifecycle(
    tree: etree._Element,
) -> tuple[date | None, date | None]:
    """Read validity from ``<lifecycle>/<eventRef>`` events (real Normattiva).

    ``in_force_from`` is the EARLIEST parseable ``@date`` among all lifecycle
    ``<eventRef>`` elements (the generation/enactment event). ``in_force_to`` is
    the date of a repeal/abrogation event — an ``<eventRef>`` whose ``@type`` or
    ``@refersTo`` (lowercased) contains one of "abrog"/"repeal"/"abol" — if any
    such dated event exists; otherwise ``None`` (still in force).

    Returns ``(None, None)`` if there are no lifecycle events with a parseable
    date. Never raises.
    """
    dates: list[date] = []
    repeal_dates: list[date] = []
    for lifecycle in tree.iter(f"{{{AKN_NS}}}lifecycle"):
        for event_ref in lifecycle.findall(f"{{{AKN_NS}}}eventRef"):
            d = _parse_date(event_ref.get("date", ""))
            if d is None:
                continue
            dates.append(d)
            marker = (
                (event_ref.get("type") or "")
                + " "
                + (event_ref.get("refersTo") or "")
            ).lower()
            if any(stem in marker for stem in _REPEAL_STEMS):
                repeal_dates.append(d)

    if not dates:
        return None, None

    in_force_from = min(dates)
    in_force_to = min(repeal_dates) if repeal_dates else None
    return in_force_from, in_force_to


def _validity_from_frbr_expression(tree: etree._Element) -> date | None:
    """Read ``identification/FRBRExpression/FRBRdate@date`` as the in-force start.

    This is the expression/in-force date Normattiva stamps on the manifestation.
    Returns ``None`` if absent or unparseable. Never raises.
    """
    frbr_date = tree.find(
        f".//{{{AKN_NS}}}identification"
        f"/{{{AKN_NS}}}FRBRExpression"
        f"/{{{AKN_NS}}}FRBRdate"
    )
    if frbr_date is None:
        return None
    return _parse_date(frbr_date.get("date", ""))


def _resolve_event_date(tree: etree._Element, ref: str | None) -> date | None:
    """Resolve a timeInterval start/end reference to a ``date``.

    Accepts both IDREF-style references (``"#e1"``) and literal date strings
    (some AKN profiles inline the date directly in the attribute).  Returns
    ``None`` on any failure without raising.
    """
    if not ref:
        return None

    rid = ref.lstrip("#")

    # Try to find a matching <akn:eventRef eId="rid">
    for event_ref in tree.iter(f"{{{AKN_NS}}}eventRef"):
        if event_ref.get("eId") == rid:
            return _parse_date(event_ref.get("date", ""))

    # Fallback: treat rid itself as a possible literal date string
    return _parse_date(rid)


def _parse_date(value: str) -> date | None:
    """Parse a date string tolerantly.

    Accepts ``"YYYY-MM-DD"`` and ISO-8601 datetime strings
    (``"YYYY-MM-DDThh:mm:ss..."``) by taking only the first 10 characters.
    Returns ``None`` on any parse failure.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def _normalize_refs(tree: etree._Element) -> list[str]:
    """Resolve native ``<akn:ref href="...">`` to OLF ids and type them.

    Real Normattiva documents cite other acts with the AKN naming-path scheme
    (``/akn/it/act/<type>/<authority>/<date>/<number>[/...][#frag]``), not
    ``urn:nir:`` strings. We handle both:

      * ``urn:nir:...`` → :func:`urnlib.to_olf_id` (ValueError → unresolved).
      * ``/akn/...``     → :func:`urnlib.akn_uri_to_olf_id` (``None`` → unresolved).
      * anything else    → unresolved.

    Resolved refs get an ``olf:target`` attribute carrying the OLF id; everything
    that cannot be mapped gets ``olf:resolution="unresolved"``. We normalize the
    *relationship*, not the text — refs are never dropped, only annotated. This
    function MUST NOT raise on any individual ref.

    Returns the resolved OLF ids in document order, deduplicated.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _collect(target: str) -> None:
        if target not in seen:
            seen.add(target)
            out.append(target)

    for ref in tree.iter(f"{{{AKN_NS}}}ref"):
        href = ref.get("href", "")
        if href.startswith("urn:nir:"):
            try:
                target = urnlib.to_olf_id(href)
            except ValueError:
                ref.set(f"{{{OLF_NS}}}resolution", "unresolved")
                continue
            ref.set(f"{{{OLF_NS}}}target", target)
            _collect(target)
        elif href.startswith("/akn/"):
            target = urnlib.akn_uri_to_olf_id(href)
            if target is not None:
                ref.set(f"{{{OLF_NS}}}target", target)
                _collect(target)
            else:
                ref.set(f"{{{OLF_NS}}}resolution", "unresolved")
        else:
            ref.set(f"{{{OLF_NS}}}resolution", "unresolved")
    return out
