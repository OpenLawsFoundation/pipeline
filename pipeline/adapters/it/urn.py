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
(``FRBRuri``). Both forms are separator-agnostic: dots (urn:nir), underscores
(FRBRuri path), and hyphens are all treated as equivalent word separators.

Canonical type resolution is a two-step pipeline:

1. ``_norm_type(t)`` — lowercase, strip, collapse any run of ``[._\\-\\s]+``
   to a single ``-``.  This makes ``decreto.del.presidente.del.consiglio.dei.ministri``,
   ``decreto_del_presidente_del_consiglio_dei_ministri``, and
   ``decreto-del-presidente-del-consiglio-dei-ministri`` all map to the same key.

2. ``_SLUG_ALIASES`` — an explicit table of well-known abbreviations that must
   match established archive slugs (``dpr``, ``dpcm``).  Every other normalised
   type maps to its own hyphenated form — no unknown-type skip, no raise.

Public API:

* :func:`to_olf_id` — parse a NIR URN and derive the OLF id via the canonical rule.
* :func:`akn_uri_to_olf_id` — resolve an AKN naming-path to an OLF id via the
  same canonical rule.

Legacy look-up tables ``_TYPE_MAP`` and ``_AKN_TYPE_TO_OLF`` are retained for
compatibility with external callers that may import them; they are no longer used
internally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Canonical separator-agnostic type resolution
# ---------------------------------------------------------------------------

def _norm_type(t: str) -> str:
    """Normalise an act-type token to a canonical hyphenated slug.

    Lowercases, strips surrounding whitespace, then collapses any run of
    separator characters (dot, underscore, hyphen, ASCII space) to a single
    hyphen.

    Examples::

        >>> _norm_type("decreto.del.presidente.del.consiglio.dei.ministri")
        'decreto-del-presidente-del-consiglio-dei-ministri'
        >>> _norm_type("decreto_del_presidente_della_repubblica")
        'decreto-del-presidente-della-repubblica'
        >>> _norm_type("decreto-legge")
        'decreto-legge'
        >>> _norm_type("decretoLegge")        # camelCase is split into words
        'decreto-legge'
    """
    # Split camelCase first — real AKN naming-path <ref> hrefs use forms like
    # "decretoDelPresidenteDellaRepubblica" — then collapse every separator.
    t = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", t.strip())
    return re.sub(r"[._\-\s]+", "-", t.lower())


# Explicit abbreviation aliases: normalised long form -> established OLF slug.
# Only entries where the slug differs from the normalised form are listed here.
_SLUG_ALIASES: dict[str, str] = {
    # DPR — three attested long forms
    "decreto-del-presidente-della-repubblica": "dpr",
    "decreto-presidente-della-repubblica":     "dpr",
    "decreto-presidente-repubblica":           "dpr",
    # DPCM — three attested long forms
    "decreto-del-presidente-del-consiglio-dei-ministri": "dpcm",
    "decreto-presidente-del-consiglio-dei-ministri":     "dpcm",
    "decreto-presidente-consiglio-ministri":             "dpcm",
}


def _type_to_slug(raw: str) -> str:
    """Return the canonical OLF type slug for *raw* (any separator form).

    Never raises.  Every input yields a deterministic, non-empty slug.
    """
    key = _norm_type(raw)
    return _SLUG_ALIASES.get(key, key)


# ---------------------------------------------------------------------------
# Legacy look-up tables (retained for import compatibility; not used internally)
# ---------------------------------------------------------------------------

# NIR act type -> OLF type slug.
_TYPE_MAP: dict[str, str] = {
    "legge": "legge",
    "decreto.legge": "decreto-legge",
    "decreto.legislativo": "decreto-legislativo",
    "decreto.presidente.repubblica": "dpr",
    "decreto.presidente.consiglio.ministri": "dpcm",
    "regio.decreto": "regio-decreto",
    "legge.costituzionale": "legge-costituzionale",
    "decreto.ministeriale": "decreto-ministeriale",
    "regio.decreto.legge": "regio-decreto-legge",
    "regio.decreto.legislativo": "regio-decreto-legislativo",
    "decreto.legislativo.luogotenenziale": "decreto-legislativo-luogotenenziale",
    "decreto.luogotenenziale": "decreto-luogotenenziale",
}

# AKN path type slug -> OLF type slug.
_AKN_TYPE_TO_OLF: dict[str, str] = {
    "legge": "legge",
    "decretolegge": "decreto-legge",
    "decreto-legge": "decreto-legge",
    "decretolegislativo": "decreto-legislativo",
    "decreto-legislativo": "decreto-legislativo",
    "decretodelpresidentedellarepubblica": "dpr",
    "decretodelpresidenedellarepu": "dpr",
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
}

# ---------------------------------------------------------------------------
# URN parsing
# ---------------------------------------------------------------------------

_URN_RE = re.compile(
    r"^urn:nir:stato:(?P<type>[a-z][a-z.\-]+):(?P<date>\d{4}-\d{2}-\d{2});(?P<num>[^~]+)"
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

    The act-type slug is resolved via :func:`_norm_type` + ``_SLUG_ALIASES``,
    so dot, underscore, and hyphen separators are all handled identically.

    Element fragments map onto the AKN eId form:
    ...;123~art2  ->  olf:it/legge/2019/123/art_2

    Raises :class:`ValueError` only for genuinely malformed URNs (parse failure).
    Every well-formed URN yields a deterministic OLF id.
    """
    p = parse(urn)
    olf_type = _type_to_slug(p.act_type)
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


def akn_uri_to_olf_id(href: str) -> str | None:
    """Resolve an AKN naming-path ``href`` to an OLF id, or ``None`` if not applicable.

    Handles the ``/akn/<jur>/act/<type>/<authority>/<date>/<number>[/...][#<frag>]``
    scheme used in Normattiva AKN exports.

    The ``<type>`` segment may use any separator style (underscores in FRBRuri,
    hyphens in older exports, camelCase in synthetic documents).  All forms are
    resolved via :func:`_norm_type` + ``_SLUG_ALIASES``.

    Returns an OLF id (``olf:it/<type>/<year>/<number>[/<eid>]``) only for
    Italian state acts (jurisdiction == "it", authority == "stato") with a full
    ``YYYY-MM-DD`` date.  Returns ``None`` — without raising — for everything
    else (EU acts, empty authority, year-only dates, non-Italian jurisdictions).
    Every well-formed Italian state-act path yields a deterministic OLF id.
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

        olf_type = _type_to_slug(akn_type)
        year = date_seg[:4]
        base = f"olf:it/{olf_type}/{year}/{number}"

        if fragment:
            # Fragment is already in eId form (e.g. "art_77"); just append.
            return f"{base}/{fragment}"
        return base
    except Exception:
        # Never raise — callers rely on None for any unresolvable href.
        return None
