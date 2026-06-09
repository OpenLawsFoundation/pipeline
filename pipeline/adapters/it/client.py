"""Client for the Normattiva Open Data API (IPZS).

Grounded in the live API (confirmed against captured responses):
  base_url = "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1"
  suffix paths start "/api/v1/...". No authentication is required.

The API exposes three capabilities this adapter uses:

  1. INCREMENTAL DISCOVERY — ``POST /api/v1/ricerca/aggiornati`` returns every act
     whose ``dataUltimaModifica`` falls in a date window (window must be <= 12
     months, <= 7000 results). No URN field is returned, and we no longer construct
     one: discovery yields the act's source *coordinates* (denominazione, anno,
     numero, codiceRedazionale, dataGU) on an ``ActRef`` whose canonical identity
     is filled in later, from the fetched AKN, during transform.

  2. BACKFILL ENUMERATION — ``POST /api/v1/ricerca/avanzata`` with just
     ``annoProvvedimento`` (no text) enumerates ALL acts of that year, paginated.
     Iterated year-by-year descending from the current UTC year down to
     ``_BACKFILL_START_YEAR``.

  3. ACT AKN RETRIEVAL — an asynchronous export flow (the only way to get AKN):
     ``ricerca-asincrona/nuova-ricerca`` (202 + token) ->
     ``ricerca-asincrona/conferma-ricerca`` (PUT) ->
     poll ``ricerca-asincrona/check-status/<token>`` until HTTP 303 carries the
     download URL in the ``x-ipzs-location`` header -> GET that URL for a ZIP of
     AKN XML.

  4. URN RESOLVER (human): ``{urn_resolver}?{native_urn}``.

The public interface the rest of the adapter depends on is unchanged:
``NormattivaClient`` with ``search_modified_since``, ``fetch_akn`` and
``resolver_url``. Construction is config-driven (config.py / config.yaml) and a
``_throttle()`` rate limiter guards every network call.
"""

from __future__ import annotations

import io
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Iterator
from xml.etree import ElementTree as ET

import requests

from ..base import ActRef
from .config import load as load_config

# Fallback constants (used only if config.yaml is absent). The live values come
# from config.py / config.yaml.
DEFAULT_BASE = "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1"
URN_RESOLVER = "https://www.normattiva.it/uri-res/N2Ls"

# AKN media type per the Akoma Ntoso standard.
AKN_MEDIA_TYPE = "application/akn+xml"

# Async-export polling defaults. Exports can take minutes; these are sane,
# good-citizen defaults overridable via config.yaml (source.export_poll_seconds /
# source.export_max_wait_seconds). Multivigente toggles full version history.
DEFAULT_EXPORT_POLL_SECONDS = 5.0
DEFAULT_EXPORT_MAX_WAIT_SECONDS = 600.0
DEFAULT_MULTIVIGENTE = True

# The aggiornati window is capped server-side at 12 months (error 1501). We chunk
# anything wider into successive <= 12-month windows. Use 360 days to stay safely
# under the 12-month ceiling regardless of month lengths / leap years.
_MAX_WINDOW_DAYS = 360

# Backfill start year: the earliest year of Italian acts (year of unification).
# Overridable via config.yaml ``source.backfill_start_year``.
_BACKFILL_START_YEAR = 1861

# AKN namespace (for parsing FRBRalias urn:nir out of an export entry).
_AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"


class NormattivaError(RuntimeError):
    """Raised for documented API error responses (server error codes, bad export
    requests, failed exports). Carries the API error code when available."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class _ThrottledError(RuntimeError):
    """Internal signal: the export submit is globally throttled (409/429) even
    after backing off. The batch loop catches this to STOP submitting cleanly and
    finish collecting whatever was already submitted — it is never propagated as a
    fatal error and never reaches the runner."""


class NormattivaClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float | None = None,
        rate_limit_s: float | None = None,
        page_size: int | None = None,
        session: requests.Session | None = None,
    ):
        cfg = load_config().source
        self.base_url = (base_url or cfg.base_url).rstrip("/")
        self.timeout = timeout if timeout is not None else cfg.request_timeout_seconds
        self.rate_limit_s = rate_limit_s if rate_limit_s is not None else cfg.rate_limit_seconds
        self.page_size = page_size if page_size is not None else cfg.page_size
        self.urn_resolver = cfg.urn_resolver
        # Optional async-export knobs (added to config with defaults; older
        # configs without them keep working via getattr fallbacks).
        self.export_poll_s = getattr(cfg, "export_poll_seconds", DEFAULT_EXPORT_POLL_SECONDS)
        self.export_max_wait_s = getattr(cfg, "export_max_wait_seconds", DEFAULT_EXPORT_MAX_WAIT_SECONDS)
        self.multivigente = getattr(cfg, "multivigente", DEFAULT_MULTIVIGENTE)
        self.backfill_start_year = getattr(cfg, "backfill_start_year", _BACKFILL_START_YEAR)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "openlawsfoundation-adapter-it"})
        self._last_call = 0.0

    def _throttle(self) -> None:
        # Be a good citizen against a public institutional API.
        wait = self.rate_limit_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    # --- discovery -------------------------------------------------------

    def search_modified_since(self, since: datetime | None) -> Iterator[ActRef]:
        """Enumerate acts changed since ``since`` (None = the whole corpus).

        ``since`` is a datetime or None:

        * not None -> INCREMENTAL via ``ricerca/aggiornati`` over the window
          ``[since, now]`` in UTC. The server caps the window at 12 months, so a
          wider span is chunked into successive <= 12-month windows, each queried
          in turn. Documented server error codes are surfaced as
          :class:`NormattivaError` (notably 1502 "too many results", a real
          signal that the caller should narrow the window).
        * None -> BACKFILL via ``ricerca/avanzata`` with ``annoProvvedimento``,
          iterating year-by-year descending from the current UTC year down to
          ``_BACKFILL_START_YEAR``, paginating each year until
          ``paginaCorrente >= numeroPagine``. Most-recent acts are yielded first.

        For each act we yield an :class:`ActRef` carrying its source COORDINATES
        (denominazione, anno, numero, codiceRedazionale, dataGU) — never a
        constructed identity. EVERY act flows through; nothing is skipped on an
        unmapped denomination, because identity is no longer inferred from the
        label. The canonical olf_id / native_urn are derived later from the
        fetched AKN's ``<FRBRWork>`` during transform.

        NOTE: a full-corpus AKN export (per-act async export) is heavy; this
        method only enumerates ``ActRef``s. Bulk AKN export optimisation is out
        of scope here.
        """
        if since is None:
            yield from self._backfill()
        else:
            yield from self._incremental(since)

    def _incremental(self, since: datetime) -> Iterator[ActRef]:
        now = datetime.now(timezone.utc)
        since = _as_utc(since)
        # Chunk [since, now] into successive <= 12-month windows.
        window_start = since
        while window_start < now:
            window_end = min(window_start + timedelta(days=_MAX_WINDOW_DAYS), now)
            self._throttle()
            body = {
                "dataInizioAggiornamento": _iso_z(window_start),
                "dataFineAggiornamento": _iso_z(window_end),
            }
            r = self.session.post(
                f"{self.base_url}/api/v1/ricerca/aggiornati",
                json=body,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            _raise_for_api_error(data)
            for ref in self._items_to_refs(data.get("listaAtti", [])):
                yield ref
            # Advance to the next window (no overlap; windows are inclusive of
            # their own end, successive windows pick up from there).
            window_start = window_end + timedelta(days=1)

    def _backfill(self) -> Iterator[ActRef]:
        current_year = datetime.now(timezone.utc).year
        for year in range(current_year, self.backfill_start_year - 1, -1):
            try:
                yield from self._backfill_year(year)
            except Exception as exc:  # noqa: BLE001 - one bad year must not abort the whole backfill
                print(
                    f"[normattiva] backfill: skipping year {year} due to error: {exc}",
                    file=sys.stderr,
                )

    def _backfill_year(self, year: int) -> Iterator[ActRef]:
        """Paginate ``ricerca/avanzata`` for a single year, yielding ActRefs."""
        page = 1
        while True:
            self._throttle()
            body = {
                "annoProvvedimento": str(year),
                "orderType": "recente",
                "paginazione": {
                    "paginaCorrente": page,
                    "numeroElementiPerPagina": self.page_size,
                },
            }
            r = self.session.post(
                f"{self.base_url}/api/v1/ricerca/avanzata",
                json=body,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            _raise_for_api_error(data)
            items = data.get("listaAtti", [])
            for ref in self._items_to_refs(items):
                yield ref
            total_pages = int(data.get("numeroPagine", 0) or 0)
            current = int(data.get("paginaCorrente", page) or page)
            if not items or current >= total_pages:
                return
            page = current + 1

    def _items_to_refs(self, items: list[dict]) -> Iterator[ActRef]:
        """Map raw search items to ActRefs carrying source coordinates only.

        Identity (olf_id / native_urn) is left ``None`` — it is derived from the
        fetched AKN during transform, not inferred from the search label. EVERY
        item yields a ref; nothing is skipped on an unmapped denomination.
        """
        for it in items:
            numero = it.get("numeroProvvedimento")
            yield ActRef(
                olf_id=None,
                native_urn=None,
                source_modified=_parse_dt(it.get("dataUltimaModifica")),
                denominazione=it.get("denominazioneAtto"),
                anno=(str(it.get("annoProvvedimento"))
                      if it.get("annoProvvedimento") is not None else None),
                numero=(str(numero) if numero is not None else None),
                codice_redazionale=it.get("codiceRedazionale"),
                data_gu=it.get("dataGU"),
            )

    # --- fetch -----------------------------------------------------------

    def fetch_akn(self, ref: ActRef) -> tuple[bytes, str]:
        """Return ``(akn_bytes, source_url)`` for one act via the async export.

        The ref's source COORDINATES (``denominazione`` / ``anno`` / ``numero``)
        are fed directly into the four-step asynchronous export — no URN
        round-trip, no label→slug guessing:

          1. POST ``ricerca-asincrona/nuova-ricerca`` -> 202 + token (UUID text).
          2. PUT ``ricerca-asincrona/conferma-ricerca`` ``{"token": ...}``.
          3. Poll GET ``ricerca-asincrona/check-status/<token>`` until HTTP 303;
             the download URL is in the ``x-ipzs-location`` response header.
          4. GET the download URL -> a ZIP of AKN XML; the entry/folder whose
             name embeds ``ref.codice_redazionale`` is selected, then the latest
             VIGENZA-≤-today version of that act is returned.

        ``source_url`` is the ``x-ipzs-location`` download URL (provenance).

        Delegates to the small reusable helpers ``_submit_export``,
        ``_check_export``, and ``_download_akn`` so the single-act and batch
        paths share one implementation of each step.
        """
        token = self._submit_export(ref)
        # Poll until done or deadline.
        deadline = time.monotonic() + self.export_max_wait_s
        url: str | None = None
        while url is None:
            self._throttle()
            url = self._check_export(token)
            if url is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Normattiva export {token} did not complete within "
                        f"{self.export_max_wait_s}s"
                    )
                self._wait_poll_interval(deadline)
        akn_bytes, source_url = self._download_akn_url(url, ref)
        return akn_bytes, source_url

    def export_batch(
        self, refs: list[ActRef]
    ) -> Iterator[tuple[ActRef, bytes, str]]:
        """Submit all export jobs up-front, then collect results as they finish.

        Because each async AKN export can take minutes on the server side,
        submitting all jobs before polling means wall-time ≈ slowest single job
        rather than O(N × per-job time).  This method is single-threaded;
        concurrency is entirely server-side.

        Phase 1 — submit all:
            For each ref call ``_submit_export`` (throttled).  A per-act submit
            failure (``NormattivaError`` / ``ValueError``) is logged and skipped —
            never fatal.  Normattiva's GLOBAL throttle, however, is a 409/429
            ``HTTPError`` on the nuova-ricerca submit: we back off a few times
            (respecting ``Retry-After``); if still throttled we STOP submitting
            further refs, log a clean-stop message, and proceed to collect
            whatever was already submitted.  The run thus exits 0 with everything
            it built — an idempotent re-run resumes from where the throttle hit.

        Phase 2 — collect:
            Loop until ``pending`` is empty or ``export_max_wait_seconds`` elapses
            (measured with ``time.monotonic``).  Each round, throttle-check every
            pending token; when ``_check_export`` returns a URL, download and
            yield ``(ref, bytes, source_url)``, then remove from pending.  After
            a full round with nothing newly ready, sleep ``export_poll_seconds``.

        Timed-out refs are logged to stderr.  They are NOT silently dropped:
        the caller will re-encounter them on the next incremental run via the
        overlap window, so this is an honest, recoverable skip.
        """
        # --- Phase 1: submit all (stop cleanly if globally throttled) ------
        pending: dict[str, ActRef] = {}  # token -> ref
        submitted = 0
        for ref in refs:
            try:
                token = self._submit_export(ref)
            except _ThrottledError as exc:
                # Global throttle: stop submitting, keep what we have.
                print(
                    f"[normattiva] batch: throttled after {submitted} submitted "
                    f"({exc}) — stopping cleanly, will resume next run",
                    file=sys.stderr,
                )
                break
            except (NormattivaError, ValueError) as exc:
                print(
                    f"[normattiva] batch: skipping {_ref_label(ref)} (submit failed): {exc}",
                    file=sys.stderr,
                )
                continue
            pending[token] = ref
            submitted += 1

        if not pending:
            return

        # --- Phase 2: collect until all done or deadline -------------------
        deadline = time.monotonic() + self.export_max_wait_s
        while pending:
            if time.monotonic() >= deadline:
                break
            newly_ready = 0
            for token, ref in list(pending.items()):
                self._throttle()
                try:
                    url = self._check_export(token)
                    if url is None:
                        continue  # still processing; try again next round
                    akn_bytes, source_url = self._download_akn_url(url, ref)
                except Exception as exc:  # noqa: BLE001
                    # Per-act poll/download error: log and drop this token. Never
                    # fatal — the act re-surfaces on the next incremental run.
                    print(
                        f"[normattiva] batch: skipping {_ref_label(ref)} "
                        f"(poll/download failed): {exc}",
                        file=sys.stderr,
                    )
                    del pending[token]
                    continue
                del pending[token]
                newly_ready += 1
                yield (ref, akn_bytes, source_url)
            if pending and newly_ready == 0:
                # Nothing became ready this round; sleep before the next sweep.
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(self.export_poll_s, remaining))

        # Any tokens still pending hit the deadline.
        if pending:
            timed_out = [_ref_label(ref) for ref in pending.values()]
            print(
                f"[normattiva] batch: export deadline reached; "
                f"{len(timed_out)} ref(s) timed out and will be retried on the "
                f"next incremental run: {timed_out}",
                file=sys.stderr,
            )

    # --- internal helpers (shared by fetch_akn and export_batch) ----------

    def _submit_export(self, ref: ActRef) -> str:
        """Submit the two-step nuova-ricerca + conferma-ricerca for one ref.

        Uses the ref's source COORDINATES (``denominazione`` / ``anno`` /
        ``numero``) directly — no URN round-trip, no label→slug guessing. EVERY
        denomination is forwarded verbatim to Normattiva's own search.

        Returns the server-issued export token (UUID string).  Raises
        ``NormattivaError`` if the API returns an error response, or
        ``_ThrottledError`` if the submit is globally throttled (409/429) even
        after backing off — the caller treats that as a clean stop signal.
        """
        denominazione = ref.denominazione
        if not denominazione:
            raise NormattivaError(
                f"ActRef has no denominazione coordinate to submit: {_ref_label(ref)}"
            )
        if not ref.anno or not ref.numero:
            raise NormattivaError(
                f"ActRef missing anno/numero coordinate to submit: {_ref_label(ref)}"
            )
        token = self._export_new_search(denominazione, ref.anno, ref.numero)
        self._export_confirm(token)
        return token

    def _check_export(self, token: str) -> str | None:
        """One check-status call for ``token``.

        Returns the ``x-ipzs-location`` download URL when HTTP 303 (done), or
        ``None`` when the export is still processing (409 / 200 / 202).  Any
        other status raises via ``raise_for_status``.

        The GET itself is wrapped against TRANSIENT network errors
        (``requests.RequestException`` — read/connect timeouts, dropped SOCKS
        reads, ...): such a failure is retried with backoff and, if still failing,
        surfaces as :class:`_ThrottledError` rather than a raw exception. The
        status-code branching is preserved: 200/202/409 are normal "still
        processing" responses here and must NOT be treated as throttle, so we use
        a non-raising GET and inspect the code ourselves.
        """
        url = f"{self.base_url}/api/v1/ricerca-asincrona/check-status/{token}"
        r = self._get_no_raise_with_backoff(url, what="check-status", allow_redirects=False)
        if r.status_code == 303:
            location = r.headers.get("x-ipzs-location")
            if not location:
                raise NormattivaError(
                    "check-status returned 303 without an x-ipzs-location header"
                )
            return location
        if r.status_code in (200, 202, 409):
            return None  # still processing
        r.raise_for_status()
        raise NormattivaError(
            f"Unexpected check-status response {r.status_code} for {token}"
        )

    def _get_no_raise_with_backoff(self, url: str, *, what: str, **kwargs):
        """GET ``url`` retrying ONLY on transient network errors, returning the
        raw response WITHOUT calling ``raise_for_status``.

        Unlike :meth:`_request_with_backoff` this never inspects the HTTP status
        (the caller does), so a 409 "still processing" is returned normally rather
        than mistaken for a throttle. A ``requests.RequestException`` is retried
        with the same backoff schedule and, once exhausted, re-raised as
        :class:`_ThrottledError` so the batch loop handles it non-fatally.
        """
        last_reason: str | None = None
        for attempt in range(len(self._THROTTLE_BACKOFFS) + 1):
            self._throttle()
            try:
                return self.session.get(url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
            if attempt >= len(self._THROTTLE_BACKOFFS):
                break
            sleep_s = self._THROTTLE_BACKOFFS[attempt]
            print(
                f"[normattiva] {what}: transient failure ({last_reason}); "
                f"backing off {sleep_s:.0f}s (attempt {attempt + 1})",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
        raise _ThrottledError(
            f"{what} still failing ({last_reason}) after "
            f"{len(self._THROTTLE_BACKOFFS)} backoffs"
        )

    def _download_akn_url(self, download_url: str, ref: ActRef) -> tuple[bytes, str]:
        """GET the ZIP at ``download_url``, extract and return ``(bytes, url)``.

        Selection matches the act by ``ref.codice_redazionale`` (embedded in the
        ZIP entry/folder names) and then the latest-VIGENZA-≤-today version; see
        :func:`_extract_akn_from_zip`.

        The GET is wrapped against transient network errors AND a throttled ZIP
        endpoint (409/429) via :meth:`_request_with_backoff`; a still-failing
        download raises :class:`_ThrottledError`, handled non-fatally by the batch
        loop (the act re-surfaces on a later run).
        """
        r = self._request_with_backoff("get", download_url, what="zip-download")
        akn_bytes = _extract_akn_from_zip(r.content, ref)
        return akn_bytes, download_url

    # Backoff schedule (seconds) for a globally-throttled or network-failed
    # request. Each entry is the wait BEFORE the next retry, so the request is
    # tried len(schedule)+1 times in total (initial attempt + one per step).
    _THROTTLE_BACKOFFS = (5.0, 10.0, 20.0)
    # HTTP status codes that mean "you are being throttled, back off".
    _THROTTLE_STATUS = (409, 429)

    def _request_with_backoff(self, method: str, url: str, *, what: str, **kwargs):
        """Issue one HTTP request, retrying on throttle (409/429) AND on any
        transient network error (``requests.RequestException`` — ReadTimeout,
        ConnectTimeout, ConnectionError, ChunkedEncodingError, ...).

        Each attempt is preceded by the good-citizen ``_throttle``. On a throttle
        status we back off the next schedule step (honoring ``Retry-After`` when
        present); on a network error we back off the same schedule. After the
        schedule is exhausted we raise :class:`_ThrottledError` carrying the last
        reason — every caller in the export flow treats that as a controlled
        stop/skip signal, NEVER letting a raw ``RequestException`` escape and kill
        the run.

        Any non-throttle ``HTTPError`` (a real 4xx/5xx that is not 409/429)
        propagates unchanged — that is a genuine per-act problem the caller maps
        to a normal skip.

        ``method`` is one of ``"get"`` / ``"post"`` / ``"put"``; ``what`` is a
        short label for the log line (e.g. ``"nuova-ricerca"``). ``kwargs`` are
        forwarded to the session method (``json=``, ``allow_redirects=``, ...).
        """
        send = getattr(self.session, method)
        last_reason: str | None = None
        for attempt in range(len(self._THROTTLE_BACKOFFS) + 1):
            self._throttle()
            retry_after: float | None = None
            try:
                r = send(url, timeout=self.timeout, **kwargs)
                r.raise_for_status()
            except requests.HTTPError as exc:
                resp = getattr(exc, "response", None)
                status = getattr(resp, "status_code", None)
                if status not in self._THROTTLE_STATUS:
                    raise  # genuine 4xx/5xx — caller handles as a per-act skip
                last_reason = f"HTTP {status}"
                retry_after = self._retry_after_seconds(resp)
            except requests.RequestException as exc:
                # Transient network failure (timeout, reset connection, broken
                # SOCKS read, ...). Back off and retry like a throttle.
                last_reason = f"{type(exc).__name__}: {exc}"
            else:
                return r
            if attempt >= len(self._THROTTLE_BACKOFFS):
                break  # schedule exhausted; give up below
            sleep_s = (
                retry_after if retry_after is not None
                else self._THROTTLE_BACKOFFS[attempt]
            )
            print(
                f"[normattiva] {what}: transient failure ({last_reason}); "
                f"backing off {sleep_s:.0f}s (attempt {attempt + 1})",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
        raise _ThrottledError(
            f"{what} still failing ({last_reason}) after "
            f"{len(self._THROTTLE_BACKOFFS)} backoffs"
        )

    def _post_with_throttle_backoff(self, url: str, *, json: dict):
        """POST ``url`` with throttle + network-error backoff (see
        :meth:`_request_with_backoff`). Kept as a thin wrapper because the submit
        path and tests refer to it by name."""
        return self._request_with_backoff("post", url, what="nuova-ricerca", json=json)

    @staticmethod
    def _retry_after_seconds(response) -> float | None:
        """Parse a ``Retry-After`` header (delta-seconds form) to a float, or None."""
        raw = response.headers.get("Retry-After") if response is not None else None
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def _export_new_search(self, denominazione: str, anno: str, numero: str) -> str:
        """POST nuova-ricerca with the act's source coordinates.

        ``denominazione`` / ``anno`` / ``numero`` come straight from the ActRef's
        search coordinates — there is no URN parse and no slug reverse-lookup.

        On Normattiva's global throttle (HTTP 409/429) this retries with
        exponential backoff (5s → 10s → 20s, a few tries, honoring ``Retry-After``
        when present); if still throttled it raises :class:`_ThrottledError` so the
        batch can stop cleanly and resume on the next run.
        """
        body = {
            "formato": "AKN",
            "tipoRicerca": "A",
            "parametriRicerca": {
                "denominazioneAtto": denominazione,
                "annoProvvedimento": str(anno),
                "numeroProvvedimento": str(numero),
                "orderType": "recente",
                "paginazione": {
                    "paginaCorrente": 1,
                    "numeroElementiPerPagina": 10,
                },
            },
        }
        body["richiestaExport"] = "M" if self.multivigente else "O"
        r = self._post_with_throttle_backoff(
            f"{self.base_url}/api/v1/ricerca-asincrona/nuova-ricerca",
            json=body,
        )
        text = r.text.strip()
        # A JSON body here means an error (e.g. {"code": 1003}); a bare UUID string
        # is the happy path.
        if text.startswith("{"):
            try:
                data = r.json()
            except ValueError:
                data = {}
            _raise_for_api_error(data)
            raise NormattivaError(f"Unexpected JSON from nuova-ricerca: {text!r}")
        if not text:
            raise NormattivaError("Empty token from nuova-ricerca")
        return text.strip('"')

    def _export_confirm(self, token: str) -> None:
        # Wrapped against throttle (409/429) AND transient network errors; a
        # still-failing confirm raises _ThrottledError, which the batch submit
        # loop treats as a clean stop-and-resume signal (never fatal).
        r = self._request_with_backoff(
            "put",
            f"{self.base_url}/api/v1/ricerca-asincrona/conferma-ricerca",
            what="conferma-ricerca",
            json={"token": token},
        )
        try:
            data = r.json()
        except ValueError:
            data = {}
        _raise_for_api_error(data)

    def _wait_poll_interval(self, deadline: float) -> None:
        """Sleep one poll interval, but never past the overall deadline."""
        remaining = deadline - time.monotonic()
        time.sleep(min(self.export_poll_s, max(0.0, remaining)))

    def resolver_url(self, native_urn: str) -> str:
        """Human-resolvable Normattiva URL for an act, for cross-checking."""
        return f"{self.urn_resolver}?{native_urn}"


# --- helpers -------------------------------------------------------------


def _raise_for_api_error(data: object) -> None:
    """Raise NormattivaError if a parsed response carries a documented error code.

    The API signals errors with a JSON ``code`` field (1003 bad request, 1501
    window too wide, 1502 too many results, 1503 end < start, ...). A 1502 is a
    real signal — the caller is expected to narrow the query — so we surface it
    rather than swallowing it.
    """
    if not isinstance(data, dict):
        return
    code = data.get("code")
    if code is None:
        return
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        code_int = None
    message = data.get("message") or data.get("descrizione") or str(data)
    raise NormattivaError(f"Normattiva API error {code}: {message}", code=code_int)


def _extract_akn_from_zip(content: bytes, ref: ActRef) -> bytes:
    """Pull the current consolidated AKN XML out of a Normattiva export ZIP.

    Normattiva ships ``richiestaExport:"M"`` (multivigente) results as a ZIP of
    SEPARATE per-version files, one per consolidation point, e.g.:

        DECRETO-LEGGE_20060403_152_..._06G00110_ORIGINALE_V0.xml
        DECRETO-LEGGE_20060403_152_..._06G00110_VIGENZA_2006-07-13_V1.xml
        DECRETO-LEGGE_20060403_152_..._06G00110_VIGENZA_2021-06-01_V141.xml
        ...

    A single export can contain MORE THAN ONE act (the search may match several);
    every entry name embeds the act's ``codiceRedazionale`` (e.g. ``_24G00035_``).
    We therefore FIRST narrow to the act we asked for by matching
    ``ref.codice_redazionale`` against the entry names, and only then pick the
    CURRENT consolidated version — the latest VIGENZA entry whose date is not in
    the future (≤ today UTC).  That file itself carries the full amendment history
    in its ``<lifecycle>``/``<analysis>`` blocks, so downstream transforms see all
    amendment events without reassembling versions.  Assembling every version into
    one temporal AKN document is a separate future concern and out of scope here.

    Selection algorithm:
      0. If ``ref.codice_redazionale`` is set and at least one entry name contains
         it, restrict the candidate entries to that act; otherwise keep all
         entries (best-effort fallback for codice-less refs / odd exports).
      1. Parse each ``_VIGENZA_<YYYY-MM-DD>_V<n>`` entry; collect (date, n, name).
      2. Keep only those with date ≤ today UTC.
      3. Pick the one with the latest date; tie-break on highest V<n>.
      4. If no VIGENZA entries pass the filter (un-amended act — only ORIGINALE),
         use the ORIGINALE_V0 entry.
      5. If filenames don't match either pattern at all, fall back to
         FRBRalias-match (against ``ref.native_urn`` when known) then first-xml.
    """
    import re

    today = datetime.now(timezone.utc).date()
    native_urn = ref.native_urn  # usually None now (identity derived later)
    codice = (ref.codice_redazionale or "").strip()

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        all_names = zf.namelist()
        all_xml_names = [n for n in all_names if n.lower().endswith(".xml")]
        if not all_xml_names:
            if not all_names:
                # Normattiva returns an empty ZIP (~22 bytes) when it has no AKN
                # for the act yet — typically a very recently published act whose
                # AKN hasn't been generated upstream. Not an error in the act or
                # our query; it will be picked up on a later run (transform is
                # idempotent, re-runs fill gaps).
                raise NormattivaError(
                    "empty export ZIP — no AKN generated upstream yet "
                    "(recently published act); will be retried on a later run"
                )
            raise NormattivaError(
                f"export ZIP has entries but no AKN .xml: {all_names[:5]}"
            )

        # Step 0: narrow to the requested act by codiceRedazionale when possible.
        xml_names = all_xml_names
        if codice:
            matched = [n for n in all_xml_names if codice.lower() in n.lower()]
            if matched:
                xml_names = matched

        if len(xml_names) == 1:
            return zf.read(xml_names[0])

        # --- parse filename tokens -------------------------------------------
        _pat_vigenza = re.compile(r"_VIGENZA_(\d{4}-\d{2}-\d{2})_V(\d+)\.xml$", re.IGNORECASE)
        _pat_originale = re.compile(r"_ORIGINALE_V(\d+)\.xml$", re.IGNORECASE)

        vigenza_entries: list[tuple[object, int, str]] = []  # (date, n, name)
        originale_name: str | None = None

        for name in xml_names:
            m = _pat_vigenza.search(name)
            if m:
                try:
                    from datetime import date as _date
                    entry_date = _date.fromisoformat(m.group(1))
                    entry_n = int(m.group(2))
                    vigenza_entries.append((entry_date, entry_n, name))
                except ValueError:
                    pass
                continue
            if _pat_originale.search(name) and originale_name is None:
                originale_name = name

        # --- select best VIGENZA ≤ today -------------------------------------
        valid_vigenza = [(d, n, nm) for (d, n, nm) in vigenza_entries if d <= today]
        if valid_vigenza:
            # latest date first, then highest V<n>
            valid_vigenza.sort(key=lambda t: (t[0], t[1]), reverse=True)
            return zf.read(valid_vigenza[0][2])

        # --- no valid VIGENZA: use ORIGINALE if found ------------------------
        if originale_name is not None:
            return zf.read(originale_name)

        # --- legacy fallback: FRBRalias-match then first-xml -----------------
        # native_urn is usually None now (identity is derived later); only do the
        # equality match when we actually have a urn to match against.
        first_bytes: bytes | None = None
        for name in xml_names:
            raw = zf.read(name)
            if first_bytes is None:
                first_bytes = raw
            if native_urn and _akn_urn(raw) == native_urn:
                return raw
        return first_bytes if first_bytes is not None else zf.read(xml_names[0])


def _akn_urn(raw: bytes) -> str | None:
    """Return the FRBRalias urn:nir value from an AKN document, or None."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    for alias in root.iter(f"{{{_AKN_NS}}}FRBRalias"):
        if alias.get("name") == "urn:nir":
            value = alias.get("value")
            if value:
                return value
    return None


def _as_utc(dt: datetime) -> datetime:
    """Coerce a datetime to UTC (treat naive datetimes as already-UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    """Format a UTC datetime as ISO8601 with milliseconds and a 'Z' suffix.

    e.g. ``2024-04-27T00:00:00.000Z`` (the shape the API expects).
    """
    dt = _as_utc(dt)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ref_label(ref: ActRef) -> str:
    """A human-readable label for logs.

    Identity (olf_id) is derived later, so before transform an ActRef carries only
    coordinates. Prefer the olf_id when present, else fall back to the coordinates
    that uniquely point at the act in the source system.
    """
    if ref.olf_id:
        return ref.olf_id
    parts = [p for p in (ref.denominazione, ref.anno, ref.numero) if p]
    coord = " ".join(parts) if parts else "?"
    if ref.codice_redazionale:
        coord += f" [{ref.codice_redazionale}]"
    return coord
