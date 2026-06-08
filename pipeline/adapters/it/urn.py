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

Identity is CANONICAL-FROM-AKN
------------------------------
Identity is derived from the fetched document, never inferred from a search label.
The fetched Akoma Ntoso carries the act's own self-id in ``<FRBRWork>`` — both as a
native NIR URN (``FRBRalias[@name="urn:nir"]``) and as an AKN naming-path
(``FRBRuri``). The transform reads those and maps the *type slug* to OLF via the
two maps below:

* :func:`to_olf_id` — NIR URN type slug → OLF slug, via ``_TYPE_MAP``.
* :func:`akn_uri_to_olf_id` — AKN-path type segment → OLF slug, via
  ``_AKN_TYPE_TO_OLF``.

Both maps are the single source of truth for the Italian act-type vocabulary and
are intentionally extensible: add a row when a previously-unseen, document-sourced
type appears. A gap is now a precise, data-driven gap (a real act type we have not
mapped) rather than a guess from a display label, so it is caught loudly at the
self-id step rather than silently corrupting identities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# NIR act type -> OLF type slug. Extend as new types appear in the corpus.
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
