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
import re
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

    # 0. declare OLF as an agent so our <proprietary source="#openlawsfoundation">
    #    resolves to a real, schema-declared <TLCOrganization>. Find-or-create,
    #    idempotent across re-runs.
    _ensure_olf_agent_declared(tree)

    # 1. identity ------------------------------------------------------
    olf_id = doc.ref.olf_id
    _set_olf_meta(tree, "identity", {
        "olfId": olf_id,
        "nativeUrn": doc.ref.native_urn,
        "jurisdiction": "it",
    })

    # 2. lifecycle events ---------------------------------------------
    # Normattiva carries multivigenza in native AKN temporal elements; we read
    # them and re-express the validity interval in the normalized OLF vocab,
    # plus the named lifecycle events we can DERIVE (never fabricate).
    in_force_from, in_force_to = _extract_validity(tree)
    lifecycle_fields = {"inForceFrom": in_force_from.isoformat() if in_force_from else None}
    if in_force_to is not None:
        lifecycle_fields["inForceTo"] = in_force_to.isoformat()
    _set_olf_meta(tree, "lifecycle", lifecycle_fields)
    _set_lifecycle_events(tree, in_force_from, in_force_to)

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

    # 5. conformance gate ---------------------------------------------
    # Honor base.py's transform contract: a document that would fail the
    # AKN4OLF suite must never reach the archive.
    _assert_conformant(akn_xml, olf_id)

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

# The agent id our <proprietary source="#openlawsfoundation"> points at. The
# leading "#" is the IDREF; the declared <TLCOrganization> carries the bare eId.
OLF_AGENT_EID = "openlawsfoundation"
OLF_AGENT_HREF = "https://openlawsfoundation.org/"
OLF_AGENT_SHOW_AS = "Open Laws Foundation"


def _ensure_olf_agent_declared(tree: etree._Element) -> None:
    """Declare the OLF organization in ``<meta>/<references>`` (find-or-create).

    Our ``<proprietary source="#openlawsfoundation">`` is an IDREF that must
    resolve to a declared agent for the document to be schema-valid AKN. AKN puts
    Top-Level-Class declarations under ``<meta>/<references>``; we ensure that
    block exists and holds a single::

        <TLCOrganization eId="openlawsfoundation"
                         href="https://openlawsfoundation.org/"
                         showAs="Open Laws Foundation"/>

    Idempotent: re-runs neither duplicate the element nor the ``<references>``
    block. Real Normattiva documents already ship a ``<references>`` element
    (holding ``<original>``); we reuse it rather than create a second one.
    """
    meta = tree.find(f".//{{{AKN_NS}}}meta")
    if meta is None:
        raise ConformanceError("Document has no <akn:meta>; not valid AKN")

    references = meta.find(f"{{{AKN_NS}}}references")
    if references is None:
        references = etree.SubElement(meta, f"{{{AKN_NS}}}references")

    for org in references.findall(f"{{{AKN_NS}}}TLCOrganization"):
        if org.get("eId") == OLF_AGENT_EID:
            # Already declared — keep it canonical, do not duplicate.
            org.set("href", OLF_AGENT_HREF)
            org.set("showAs", OLF_AGENT_SHOW_AS)
            return

    org = etree.SubElement(references, f"{{{AKN_NS}}}TLCOrganization")
    org.set("eId", OLF_AGENT_EID)
    org.set("href", OLF_AGENT_HREF)
    org.set("showAs", OLF_AGENT_SHOW_AS)


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


def _set_olf_meta(tree: etree._Element, group: str, fields: dict[str, str | None]) -> None:
    """Write an ``<olf:GROUP>`` element with the given attributes.

    Attributes whose value is ``None`` or empty are NOT emitted (an empty string
    is not a valid date/value); any such attribute already present from a prior
    run is removed, so re-runs converge on the same clean output.
    """
    olf = _olf_block(tree)
    el = olf.find(f"{{{OLF_NS}}}{group}")
    if el is None:
        el = etree.SubElement(olf, f"{{{OLF_NS}}}{group}")
    for k, v in fields.items():
        if v:
            el.set(k, v)
        elif k in el.attrib:
            del el.attrib[k]


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


# --- lifecycle events (named vocabulary) -----------------------------

def _set_lifecycle_events(
    tree: etree._Element,
    in_force_from: date | None,
    in_force_to: date | None,
) -> None:
    """Emit ``<olf:event type=".." date="YYYY-MM-DD"/>`` for derivable events.

    We NEVER fabricate a date; an event is emitted only when its date is actually
    present in the source. Derivation, per AKN4OLF vocabulary:

      * ``enacted``   — the act emanation date. Source priority:
        ``identification/FRBRWork/FRBRdate@date`` → the URN/FRBRalias date →
        the earliest ``<lifecycle>/<eventRef>`` date (the generation event).
      * ``published`` — the Gazzetta Ufficiale publication date:
        ``<meta>/<publication>@date`` if present, else
        ``identification/FRBRExpression/FRBRdate@date``.
      * ``commenced`` — entry into force = the layered ``in_force_from`` already
        computed by :func:`_extract_validity` (kept identical to
        ``inForceFrom`` so the interval and the named event agree).
      * ``amended``   — each distinct amendment date we can find: lifecycle
        ``<eventRef>`` elements whose ``@type``/``@refersTo`` mark an amendment,
        plus the ``@date`` of any ``<passiveModifications>`` modification.
        Emitted de-duplicated and sorted, one ``<olf:event>`` per distinct date.
      * ``repealed``  — emitted only when an end-of-validity (abrogation) date is
        detectable, i.e. the derived ``in_force_to``; otherwise omitted.

    Children are rebuilt on every call (cleared first) so re-runs are idempotent.
    """
    olf = _olf_block(tree)
    lifecycle = olf.find(f"{{{OLF_NS}}}lifecycle")
    if lifecycle is None:
        lifecycle = etree.SubElement(olf, f"{{{OLF_NS}}}lifecycle")

    # Idempotency: drop any events from a previous run before re-deriving.
    for prev in lifecycle.findall(f"{{{OLF_NS}}}event"):
        lifecycle.remove(prev)

    def emit(event_type: str, d: date | None) -> None:
        if d is None:
            return
        ev = etree.SubElement(lifecycle, f"{{{OLF_NS}}}event")
        ev.set("type", event_type)
        ev.set("date", d.isoformat())

    emit("enacted", _derive_enacted(tree))
    emit("published", _derive_published(tree))
    emit("commenced", in_force_from)
    for d in _derive_amended_dates(tree):
        emit("amended", d)
    emit("repealed", in_force_to)


def _derive_enacted(tree: etree._Element) -> date | None:
    """The act emanation date: FRBRWork/FRBRdate → URN date → first eventRef."""
    work_date = tree.find(
        f".//{{{AKN_NS}}}identification"
        f"/{{{AKN_NS}}}FRBRWork"
        f"/{{{AKN_NS}}}FRBRdate"
    )
    if work_date is not None:
        d = _parse_date(work_date.get("date", ""))
        if d is not None:
            return d

    # URN / FRBRalias date (urn:nir:...:YYYY-MM-DD;num).
    for alias in tree.iter(f"{{{AKN_NS}}}FRBRalias"):
        if alias.get("name") == "urn:nir":
            d = _urn_date(alias.get("value", ""))
            if d is not None:
                return d

    # Earliest lifecycle eventRef = the generation event.
    dates = [
        d
        for lc in tree.iter(f"{{{AKN_NS}}}lifecycle")
        for ev in lc.findall(f"{{{AKN_NS}}}eventRef")
        if (d := _parse_date(ev.get("date", ""))) is not None
    ]
    return min(dates) if dates else None


def _urn_date(urn: str) -> date | None:
    """Pull the YYYY-MM-DD emanation date out of a NIR URN, tolerantly."""
    m = re.search(r":(\d{4}-\d{2}-\d{2});", urn)
    return _parse_date(m.group(1)) if m else None


def _derive_published(tree: etree._Element) -> date | None:
    """The Gazzetta Ufficiale publication date: <publication>@date else FRBRExpr."""
    pub = tree.find(f".//{{{AKN_NS}}}meta/{{{AKN_NS}}}publication")
    if pub is not None:
        d = _parse_date(pub.get("date", ""))
        if d is not None:
            return d
    return _validity_from_frbr_expression(tree)


# Event-type/refersTo stems (lowercased) that mark an AMENDMENT lifecycle event.
_AMEND_STEMS = ("amend", "modif", "novell", "aggiorn")


def _passiveref_eids(tree: etree._Element) -> set[str]:
    """Collect eId values of ``<passiveRef>`` elements in ``<meta>/<references>``.

    In real Normattiva consolidated documents (multivigente export), lifecycle
    ``<eventRef>`` elements carry no ``@type``/``@refersTo`` — their only
    discriminator is the ``@source`` attribute, which is an IDREF into
    ``<meta>/<references>``.  A source that resolves to a ``<passiveRef>``
    (another act that modified this one) is by definition an amendment event.
    The original act's own enactment is represented by ``<original>``.
    """
    eids: set[str] = set()
    for meta in tree.iter(f"{{{AKN_NS}}}meta"):
        for refs in meta.findall(f"{{{AKN_NS}}}references"):
            for pr in refs.findall(f"{{{AKN_NS}}}passiveRef"):
                eid = pr.get("eId")
                if eid:
                    eids.add(eid)
    return eids


def _derive_amended_dates(tree: etree._Element) -> list[date]:
    """Distinct, sorted amendment dates derivable from the source.

    Three sources, unioned and de-duplicated:

      a. lifecycle ``<eventRef>`` whose ``@type``/``@refersTo`` (lowercased)
         names an amendment (EN *amend*/*modif*, IT *modif*/*novell*/*aggiorn*).
         This covers the idealized AKN / synthetic corpus.

      b. lifecycle ``<eventRef>`` whose ``@source`` IDREF resolves to a
         ``<passiveRef>`` in ``<meta>/<references>``. Real Normattiva
         consolidated files omit ``@type``/``@refersTo`` entirely but always
         annotate each amending act in ``<references>`` as a ``<passiveRef>``.
         Every such eventRef IS an amendment event; the ``<original>`` ref
         (``@source`` resolves to an ``<original>`` element) is the enactment
         itself and is therefore excluded.

      c. ``@date`` on every modification element directly under
         ``<passiveModifications>``. In some exports these carry inline dates
         rather than referencing lifecycle events.
    """
    found: set[date] = set()

    # Build the passiveRef eId set for source (b).
    passive_eids = _passiveref_eids(tree)

    for lc in tree.iter(f"{{{AKN_NS}}}lifecycle"):
        for ev in lc.findall(f"{{{AKN_NS}}}eventRef"):
            # Source (a): explicit type/refersTo stems.
            marker = ((ev.get("type") or "") + " " + (ev.get("refersTo") or "")).lower()
            is_amend_by_type = any(stem in marker for stem in _AMEND_STEMS)

            # Source (b): @source IDREF → passiveRef.
            source_ref = (ev.get("source") or "").lstrip("#")
            is_amend_by_source = bool(source_ref and source_ref in passive_eids)

            if is_amend_by_type or is_amend_by_source:
                d = _parse_date(ev.get("date", ""))
                if d is not None:
                    found.add(d)

    # Source (c): inline dates on passiveModification children.
    for pm in tree.iter(f"{{{AKN_NS}}}passiveModifications"):
        for mod in pm:
            d = _parse_date(mod.get("date", ""))
            if d is not None:
                found.add(d)

    return sorted(found)


# --- typed reference relations ---------------------------------------

# Modification @type stems (lowercased) that mean an end-of-life REPEAL.
_RELATION_REPEAL_STEMS = ("repeal", "abrog")


def _act_level_olf_id(olf_id: str) -> str:
    """Strip a trailing ``/<eId>`` element fragment to get the ACT-LEVEL id.

    ``olf:it/legge/2021/234/art_1__para_977`` → ``olf:it/legge/2021/234``.
    An id that is already act-level (4 path segments after ``olf:``) is returned
    unchanged.
    """
    body = olf_id[len("olf:"):] if olf_id.startswith("olf:") else olf_id
    segments = body.split("/")
    # olf:<jur>/<type>/<year>/<number>[/<eId>...]
    act_segments = segments[:4]
    return "olf:" + "/".join(act_segments)


def _resolve_href_to_act_level(href: str) -> str | None:
    """Resolve a modification destination href to an ACT-LEVEL OLF id, or None.

    The href is first stripped of any ``#...`` and ``/~...`` (or bare ``~...``)
    element fragment, then resolved with the scheme-appropriate mapper:

      * ``urn:nir:...`` → :func:`urnlib.to_olf_id`
      * ``/akn/...``     → :func:`urnlib.akn_uri_to_olf_id`

    Any failure (unmapped type, malformed path, etc.) yields ``None`` — the
    caller skips unresolvable destinations rather than guessing.
    """
    if not href:
        return None
    # Strip element fragments in either notation, then any trailing slash.
    stripped = href.split("#", 1)[0]
    stripped = stripped.split("~", 1)[0]
    stripped = stripped.rstrip("/")
    try:
        if stripped.startswith("urn:nir:"):
            return _act_level_olf_id(urnlib.to_olf_id(stripped))
        if stripped.startswith("/akn/"):
            olf = urnlib.akn_uri_to_olf_id(stripped)
            return _act_level_olf_id(olf) if olf else None
    except Exception:
        return None
    return None


def _build_relation_maps(tree: etree._Element) -> tuple[set[str], set[str]]:
    """Bucket modification destinations into (REPEALS, AMENDS) act-level OLF ids.

    Reads ``<meta>/<analysis>/<activeModifications>`` and, when present,
    ``<passiveModifications>`` (a passive modification is still a typed relation
    to the other act). For each modification element we read ``@type`` and
    resolve its ``<destination>/@href`` to an act-level OLF id:

      * ``@type`` containing "repeal"/"abrog" → REPEALS
      * any other modification type           → AMENDS

    A destination that resolves into both buckets is treated as a REPEAL
    (repeal dominates amend). Unresolvable destinations are skipped.
    """
    repeals: set[str] = set()
    amends: set[str] = set()

    containers = list(tree.iter(f"{{{AKN_NS}}}activeModifications"))
    containers += list(tree.iter(f"{{{AKN_NS}}}passiveModifications"))

    for container in containers:
        for mod in container:
            mod_type = (mod.get("type") or "").lower()
            dest = mod.find(f"{{{AKN_NS}}}destination")
            if dest is None:
                continue
            act_id = _resolve_href_to_act_level(dest.get("href", ""))
            if act_id is None:
                continue
            if any(stem in mod_type for stem in _RELATION_REPEAL_STEMS):
                repeals.add(act_id)
            else:
                amends.add(act_id)

    # Repeal dominates: don't also report the same act as merely amended.
    amends -= repeals
    return repeals, amends


def _normalize_refs(tree: etree._Element) -> list[str]:
    """Resolve native ``<akn:ref href="...">`` to OLF ids, type them, relate them.

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

    On each resolved ref we additionally stamp an ``olf:relation``, computed from
    the ref's ACT-LEVEL OLF id against the modification maps built from
    ``<analysis>`` (see :func:`_build_relation_maps`):

      * act-level id in REPEALS          → ``"repeals"``
      * elif in AMENDS                   → ``"amends"``
      * elif the ref targets an EU act   → ``"implements"``
        (AKN path type ``direttivaUe`` or authority ``eu``)
      * else                             → ``"cites"``

    Unresolved refs carry no relation (resolution-or-unresolved, nothing more).

    Returns the resolved OLF ids in document order, deduplicated.
    """
    repeals, amends = _build_relation_maps(tree)

    out: list[str] = []
    seen: set[str] = set()

    def _collect(target: str) -> None:
        if target not in seen:
            seen.add(target)
            out.append(target)

    def _relation_for(target: str, href: str) -> str:
        act_id = _act_level_olf_id(target)
        if act_id in repeals:
            return "repeals"
        if act_id in amends:
            return "amends"
        if _is_eu_ref(href):
            return "implements"
        return "cites"

    for ref in tree.iter(f"{{{AKN_NS}}}ref"):
        href = ref.get("href", "")
        if href.startswith("urn:nir:"):
            try:
                target = urnlib.to_olf_id(href)
            except ValueError:
                ref.set(f"{{{OLF_NS}}}resolution", "unresolved")
                continue
            ref.set(f"{{{OLF_NS}}}target", target)
            ref.set(f"{{{OLF_NS}}}relation", _relation_for(target, href))
            _collect(target)
        elif href.startswith("/akn/"):
            target = urnlib.akn_uri_to_olf_id(href)
            if target is not None:
                ref.set(f"{{{OLF_NS}}}target", target)
                ref.set(f"{{{OLF_NS}}}relation", _relation_for(target, href))
                _collect(target)
            else:
                ref.set(f"{{{OLF_NS}}}resolution", "unresolved")
        else:
            ref.set(f"{{{OLF_NS}}}resolution", "unresolved")
    return out


def _is_eu_ref(href: str) -> bool:
    """True if an AKN-path href targets an EU act (implements relation).

    Matches the ``/akn/<jur>/act/<type>/<authority>/...`` shape where either the
    act type is an EU instrument (``direttivaue``, ``regolamentoue``, ...) or the
    authority segment is ``eu``.
    """
    path = href.split("#", 1)[0]
    if not path.startswith("/akn/"):
        return False
    segments = path.lstrip("/").split("/")
    if len(segments) < 5:
        return False
    # akn / <jur> / act / <type> / <authority> / ...
    akn_type = segments[3].lower()
    authority = segments[4].lower()
    if authority == "eu":
        return True
    if akn_type.endswith("ue") or "direttiva" in akn_type:
        return True
    return False


# --- conformance gate ------------------------------------------------

_OLF_ID_RE = re.compile(r"^olf:[a-z]{2}/")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _assert_conformant(akn_xml: bytes, olf_id: str) -> None:
    """Lightweight in-adapter AKN4OLF gate; raise on the first failing criterion.

    This honors base.py's transform contract — a document that would fail the
    suite must never reach the archive. It is intentionally a subset of the full
    reusable conformance suite (which lives in the spec repo); it checks only the
    criteria we can verify cheaply from the serialized output:

      1. the output re-parses as well-formed XML and has ``<akn:meta>``
         (schema-valid-ish);
      2. ``<olf:identity>`` exists with ``@olfId`` matching ``^olf:[a-z]{2}/``;
      3. ``<olf:provenance>`` has a 64-hex ``@sourceSha256``, a non-empty
         ``@adapter``, and a ``@generatedAt`` ending in ``Z``;
      4. every ``<akn:ref>`` carries either ``olf:target`` or
         ``olf:resolution="unresolved"`` (refs resolve-or-unresolved);
      5. if both ``inForceFrom`` and ``inForceTo`` are present →
         ``inForceFrom <= inForceTo`` (temporal monotonicity);
      6. the ``<proprietary source="#openlawsfoundation">`` agent is declared
         (a ``TLCOrganization eId="openlawsfoundation"`` exists).

    Raises:
        ConformanceError: naming the first failing criterion.
    """
    # Criterion 1: well-formed + has <akn:meta>.
    try:
        tree = etree.fromstring(akn_xml)
    except etree.XMLSyntaxError as e:
        raise ConformanceError(
            f"conformance[1 well-formed]: output is not well-formed XML: {e}"
        ) from e
    if tree.find(f".//{{{AKN_NS}}}meta") is None:
        raise ConformanceError(
            "conformance[1 schema]: output has no <akn:meta>; not valid AKN"
        )

    # Criterion 2: <olf:identity>/@olfId.
    identity = tree.find(f".//{{{OLF_NS}}}identity")
    if identity is None:
        raise ConformanceError("conformance[2 identity]: <olf:identity> is missing")
    olf_attr = identity.get("olfId", "")
    if not _OLF_ID_RE.match(olf_attr):
        raise ConformanceError(
            f"conformance[2 identity]: @olfId {olf_attr!r} does not match ^olf:[a-z]{{2}}/"
        )

    # Criterion 3: <olf:provenance>.
    prov = tree.find(f".//{{{OLF_NS}}}provenance")
    if prov is None:
        raise ConformanceError("conformance[3 provenance]: <olf:provenance> is missing")
    sha = prov.get("sourceSha256", "")
    if not _SHA256_RE.match(sha):
        raise ConformanceError(
            f"conformance[3 provenance]: @sourceSha256 {sha!r} is not 64 hex chars"
        )
    if not (prov.get("adapter") or "").strip():
        raise ConformanceError(
            "conformance[3 provenance]: @adapter is empty"
        )
    gen = prov.get("generatedAt", "")
    if not gen.endswith("Z"):
        raise ConformanceError(
            f"conformance[3 provenance]: @generatedAt {gen!r} does not end in 'Z'"
        )

    # Criterion 4: every <akn:ref> resolves-or-unresolved.
    for ref in tree.iter(f"{{{AKN_NS}}}ref"):
        has_target = ref.get(f"{{{OLF_NS}}}target") is not None
        unresolved = ref.get(f"{{{OLF_NS}}}resolution") == "unresolved"
        if not (has_target or unresolved):
            raise ConformanceError(
                f"conformance[4 refs]: <ref href={ref.get('href')!r}> has neither "
                f"olf:target nor olf:resolution='unresolved'"
            )

    # Criterion 5: temporal monotonicity (only when both bounds present).
    lifecycle = tree.find(f".//{{{OLF_NS}}}lifecycle")
    if lifecycle is not None:
        ff = _parse_date(lifecycle.get("inForceFrom", ""))
        ft = _parse_date(lifecycle.get("inForceTo", ""))
        if ff is not None and ft is not None and ff > ft:
            raise ConformanceError(
                f"conformance[5 temporal]: inForceFrom {ff.isoformat()} > "
                f"inForceTo {ft.isoformat()} (non-monotonic)"
            )

    # Criterion 6: the OLF proprietary agent is declared.
    declared = False
    for org in tree.iter(f"{{{AKN_NS}}}TLCOrganization"):
        if org.get("eId") == OLF_AGENT_EID:
            declared = True
            break
    if not declared:
        raise ConformanceError(
            f"conformance[6 agent]: <proprietary source={OLF_PROPRIETARY_SOURCE!r}> "
            f"points at an undeclared agent (no TLCOrganization eId={OLF_AGENT_EID!r})"
        )
