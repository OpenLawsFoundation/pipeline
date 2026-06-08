"""NIR URN  <->  OLF id mapping for Italy.

Normattiva addresses acts with NIR ("Norme in Rete") URNs:

    urn:nir:stato:legge:2007-12-24;244
    urn:nir:stato:decreto.legislativo:2003-06-30;196
    urn:nir:stato:legge:2007-12-24;244~art2-com428   (element fragment)

We map the act-level URN to a stable OLF id and keep the native URN alongside it
in the document metadata. The OLF id is what makes the archive navigable by
jurisdiction; the URN is what resolves against Normattiva.

NOTE: this is the AKN4OLF identity normalization in practice — we normalize the
*name*, never the content.

Design notes on the act-type vocabulary
----------------------------------------
``_DENOMINAZIONE_TO_NIR`` and ``_TYPE_MAP`` are the single source of truth for
the Italian act-type vocabulary.  Both maps are intentionally extensible: add a
new row to each when a previously-unseen denominazione appears in the corpus.

The public helper :func:`denominazione_for_nir_slug` is the canonical reverse
lookup (NIR slug → API denominazione); client code must use it instead of
maintaining a private copy.  Any slug not present in the maps raises ``ValueError``
— loud failure on purpose, so gaps are found quickly rather than silently
corrupting act identities.

When a denominazione is encountered at ingest time that is not yet in the map,
the adapter skips that act with a warning to stderr.  That is an honest,
safe degradation — NOT a silent drop — and the act can be re-ingested once the
vocabulary is extended.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# NIR act type -> OLF type slug. Extend as new types appear in the corpus.
# Each entry here must have a corresponding entry in _DENOMINAZIONE_TO_NIR.
_TYPE_MAP: dict[str, str] = {
    "legge": "legge",
    "decreto.legge": "decreto-legge",
    "decreto.legislativo": "decreto-legislativo",
    "decreto.presidente.repubblica": "dpr",
    "decreto.presidente.consiglio.ministri": "dpcm",
    "regio.decreto": "regio-decreto",
    "legge.costituzionale": "legge-costituzionale",
    "decreto.ministeriale": "decreto-ministeriale",
    # Historical / transitional act types (confidence: high — standard NIR usage)
    "regio.decreto.legge": "regio-decreto-legge",
    "regio.decreto.legislativo": "regio-decreto-legislativo",
    "decreto.legislativo.luogotenenziale": "decreto-legislativo-luogotenenziale",
    "decreto.luogotenenziale": "decreto-luogotenenziale",
}

# Normattiva API denominazioneAtto -> NIR type slug.
# Keys are the uppercase strings returned by the ricerca/aggiornati endpoint;
# matching is case-insensitive with normalised internal whitespace.
# Each entry here must have a corresponding entry in _TYPE_MAP.
# NOTE: every NIR slug must appear at most ONCE as a value here — the inverse
# map (_NIR_TO_DENOMINAZIONE, built below) must be a true bijection.
_DENOMINAZIONE_TO_NIR: dict[str, str] = {
    "LEGGE": "legge",
    "DECRETO-LEGGE": "decreto.legge",
    "DECRETO LEGISLATIVO": "decreto.legislativo",
    "DECRETO DEL PRESIDENTE DELLA REPUBBLICA": "decreto.presidente.repubblica",
    "DECRETO DEL PRESIDENTE DEL CONSIGLIO DEI MINISTRI": "decreto.presidente.consiglio.ministri",
    "REGIO DECRETO": "regio.decreto",
    "LEGGE COSTITUZIONALE": "legge.costituzionale",
    "DECRETO MINISTERIALE": "decreto.ministeriale",
    # Historical / transitional act types (confidence: high — standard NIR usage)
    "REGIO DECRETO-LEGGE": "regio.decreto.legge",
    "REGIO DECRETO LEGISLATIVO": "regio.decreto.legislativo",
    "DECRETO LEGISLATIVO LUOGOTENENZIALE": "decreto.legislativo.luogotenenziale",
    "DECRETO LUOGOTENENZIALE": "decreto.luogotenenziale",
}

# Inverse of _DENOMINAZIONE_TO_NIR: NIR slug -> canonical API denominazione.
# Built once at module load; collisions (two denominazioni mapping to the same
# slug) would silently drop one entry and are caught by the assertion below.
_NIR_TO_DENOMINAZIONE: dict[str, str] = {
    slug: denom for denom, slug in _DENOMINAZIONE_TO_NIR.items()
}
assert len(_NIR_TO_DENOMINAZIONE) == len(_DENOMINAZIONE_TO_NIR), (
    "Collision in _DENOMINAZIONE_TO_NIR: two denominazioni share the same NIR slug. "
    "Each NIR slug must map back to exactly one denominazione."
)

# AKN path type slug (as used in /akn/<jur>/act/<type>/...) -> OLF type slug.
# AKN path types may be camelCase or hyphen-separated; we normalise to lowercase
# before lookup.  The OLF slug is what goes into the olf:it/... id.
_AKN_TYPE_TO_OLF: dict[str, str] = {
    # exact lowercase of the camelCase / hyphen forms seen in real data
    "legge": "legge",
    "decretolegge": "decreto-legge",          # decretoLegge -> lower
    "decreto-legge": "decreto-legge",
    "decretolegislativo": "decreto-legislativo",
    "decreto-legislativo": "decreto-legislativo",
    # camelCase forms seen in real Normattiva AKN exports (lowercased + stripped)
    "decretodelpresidentedellarepubblica": "dpr",
    "decretodelpresidenedellarepu": "dpr",    # defensive alias
    "decretodelpresidenedellarepubb": "dpr",
    "decretodelpresidenedelconsiglio": "dpcm",
    "decreto": "decreto",
    "regiodecreto": "regio-decreto",
    "regio.decreto": "regio-decreto",
    "regio-decreto": "regio-decreto",
    "leggecostituzionale": "legge-costituzionale",
    "legge-costituzionale": "legge-costituzionale",
    "legge.costituzionale": "legge-costituzionale",
    "decretoministero": "decreto-ministeriale",
    "decreto.ministeriale": "decreto-ministeriale",
    "decreto-ministeriale": "decreto-ministeriale",
    "costituzione": "costituzione",
    # compound types seen in real corpus (state acts)
    "decretodelpresidenedellarepubb": "dpr",
    "decretodelpresidenedelconsiglio": "dpcm",
}

_URN_RE = re.compile(
    r"^urn:nir:stato:(?P<type>[a-z.]+):(?P<date>\d{4}-\d{2}-\d{2});(?P<num>[^~]+)"
    r"(?:~(?P<frag>.+))?$"
)

# Matches a full ISO date (YYYY-MM-DD) used in AKN paths.
_FULL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class ParsedUrn:
    act_type: str   # native NIR type, e.g. "decreto.legislativo"
    year: int
    number: str
    fragment: str | None  # e.g. "art2-com428", or None for the whole act


def parse(urn: str) -> ParsedUrn:
    m = _URN_RE.match(urn.strip())
    if not m:
        raise ValueError(f"Not a recognized NIR URN: {urn!r}")
    return ParsedUrn(
        act_type=m["type"],
        year=int(m["date"][:4]),
        number=m["num"],
        fragment=m["frag"],
    )


def to_olf_id(urn: str) -> str:
    """urn:nir:stato:legge:2019-08-05;123  ->  olf:it/legge/2019/123

    Element fragments map onto the AKN eId form:
    ...;123~art2  ->  olf:it/legge/2019/123/art_2
    """
    p = parse(urn)
    olf_type = _TYPE_MAP.get(p.act_type)
    if olf_type is None:
        # Unknown type must fail loudly, not silently produce a wrong id.
        raise ValueError(f"Unmapped NIR act type {p.act_type!r}; add it to _TYPE_MAP")

    base = f"olf:it/{olf_type}/{p.year}/{p.number}"
    if p.fragment:
        return f"{base}/{_fragment_to_eid(p.fragment)}"
    return base


def _fragment_to_eid(fragment: str) -> str:
    """art2-com428 -> art_2__com_428 (AKN eId convention).

    Kept deliberately simple; the conformance suite checks eId stability, so this
    mapping must be deterministic and reversible-enough for navigation.
    """
    parts = fragment.split("-")
    out = []
    for part in parts:
        m = re.match(r"^([a-z]+)(\d+)$", part)
        out.append(f"{m[1]}_{m[2]}" if m else part)
    return "__".join(out)


def denominazione_for_nir_slug(slug: str) -> str:
    """Return the Normattiva API ``denominazioneAtto`` for a NIR type slug.

    This is the public, canonical reverse lookup — the inverse of
    ``_DENOMINAZIONE_TO_NIR``.  Client code must use this function instead of
    maintaining a private copy of the reverse map.

    Raises :class:`ValueError` for any slug not present in the vocabulary
    (loud failure by design — unknown slugs must be added explicitly rather
    than silently producing wrong API calls).
    """
    denom = _NIR_TO_DENOMINAZIONE.get(slug)
    if denom is None:
        raise ValueError(
            f"No Normattiva denominazione mapped for NIR slug {slug!r}; "
            f"add it to _DENOMINAZIONE_TO_NIR (and _TYPE_MAP) in urn.py"
        )
    return denom


def build_nir_urn(
    denominazione: str,
    year: "str | int",
    month: "str | int",
    day: "str | int",
    number: str,
    authority: str = "stato",
) -> str:
    """Construct a NIR URN from Normattiva search-result fields.

    Parameters match the fields returned by the ricerca/aggiornati API:
    - denominazione: ``denominazioneAtto`` value, e.g. "DECRETO-LEGGE"
    - year/month/day: ``annoProvvedimento`` / ``meseProvvedimento`` / ``giornoProvvedimento``
    - number: ``numeroProvvedimento``
    - authority: defaults to "stato"

    Returns e.g. ``urn:nir:stato:decreto.legge:2024-03-02;19``.

    The result is guaranteed to be parseable by :func:`parse` and convertible by
    :func:`to_olf_id` for all denominations that are already in ``_TYPE_MAP``.

    Raises :class:`ValueError` for unrecognised ``denominazione`` values (loud
    failure by design — unknown types must be added explicitly).
    """
    # Normalise: uppercase, collapse internal whitespace.
    key = " ".join(denominazione.upper().split())
    nir_slug = _DENOMINAZIONE_TO_NIR.get(key)
    if nir_slug is None:
        raise ValueError(
            f"Unrecognised denominazioneAtto {denominazione!r}; "
            f"add it to _DENOMINAZIONE_TO_NIR"
        )
    date_str = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return f"urn:nir:{authority}:{nir_slug}:{date_str};{number}"


def _akn_type_to_olf(akn_type: str) -> str | None:
    """Map an AKN path type segment to an OLF type slug, or None if unknown.

    The AKN path types may be camelCase (e.g. "decretoLegge") or hyphen/dot
    separated (e.g. "decreto-legge").  We normalise by lowercasing and stripping
    all separators before lookup, falling back to the lowercased original.
    """
    lower = akn_type.lower()
    # Direct lookup first (handles hyphen/dot forms already in the map).
    if lower in _AKN_TYPE_TO_OLF:
        return _AKN_TYPE_TO_OLF[lower]
    # Strip all non-alpha characters and try again (handles camelCase → run-together).
    stripped = re.sub(r"[^a-z]", "", lower)
    if stripped in _AKN_TYPE_TO_OLF:
        return _AKN_TYPE_TO_OLF[stripped]
    return None


def akn_uri_to_olf_id(href: str) -> str | None:
    """Resolve an AKN naming-path ``href`` to an OLF id, or ``None`` if not applicable.

    Handles the ``/akn/<jur>/act/<type>/<authority>/<date>/<number>[/...][#<frag>]``
    scheme used in Normattiva AKN exports.

    Returns an OLF id (``olf:it/<type>/<year>/<number>[/<eid>]``) only for
    Italian state acts (jurisdiction == "it", authority == "stato") with a full
    ``YYYY-MM-DD`` date.  Returns ``None`` — without raising — for everything
    else (EU acts, empty authority, year-only dates, unmapped act types, etc.).
    """
    try:
        # Split fragment first.
        if "#" in href:
            path, fragment = href.split("#", 1)
        else:
            path, fragment = href, ""

        # Must start with /akn/
        if not path.startswith("/akn/"):
            return None

        # Strip leading slash and split.
        segments = path.lstrip("/").split("/")
        # Expected: akn / <jur> / act / <type> / <authority> / <date> / <number> [/ ...]
        if len(segments) < 7:
            return None

        _, jur, literal_act, akn_type, authority, date_seg, number = segments[:7]

        if jur != "it":
            return None
        if literal_act != "act":
            return None
        if authority != "stato":
            return None
        if not _FULL_DATE_RE.match(date_seg):
            return None

        olf_type = _akn_type_to_olf(akn_type)
        if olf_type is None:
            return None

        year = date_seg[:4]
        base = f"olf:it/{olf_type}/{year}/{number}"

        if fragment:
            # Fragment is already in eId form (e.g. "art_77"); just append.
            return f"{base}/{fragment}"
        return base
    except Exception:
        # Never raise — callers rely on None for any unresolvable href.
        return None
