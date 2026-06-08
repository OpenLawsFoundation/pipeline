"""Generic runner. Loads a jurisdiction adapter, walks discover->fetch->transform,
and writes AKN4OLF documents into a local checkout of the `archive` repo.

It does NOT commit. Committing/PRing is the workflow's job, so the same runner
works locally, on a real backfill runner, and inside GitHub Actions.

Layout written into the archive:

    <archive>/it/<olf-type>/<year>/<number>.akn.xml

Usage (must be run from the inner `pipeline/` directory that contains `adapters/`):
    python -m adapters.runner.run --jurisdiction it --archive /path/to/archive [--since ISO8601]
    python -m adapters.runner.run --jurisdiction it --archive /path/to/archive --backfill
"""

from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime
from pathlib import Path

from adapters.base import Adapter, ConformanceError


def load_adapter(jurisdiction: str) -> Adapter:
    mod = importlib.import_module(f"adapters.{jurisdiction}.adapter")
    return mod.get_adapter()


def archive_path(archive_root: Path, olf_id: str) -> Path:
    # olf:it/legge/2019/123  ->  it/legge/2019/123.akn.xml
    body = olf_id.split(":", 1)[1]
    return archive_root / (body + ".akn.xml")


def run(jurisdiction: str, archive_root: Path, since: datetime | None) -> int:
    adapter = load_adapter(jurisdiction)
    print(f"adapter {adapter.name}@{adapter.version} | since={since or 'BACKFILL'}")

    # Materialise the full ref list so we know the total count up-front (needed
    # for the summary) and so we can hand the whole list to fetch_many at once.
    refs = list(adapter.discover(since))
    total = len(refs)

    written = errors = 0

    def _write_doc(source_doc) -> None:
        """Transform one SourceDocument and write it; updates written/errors."""
        nonlocal written, errors
        try:
            doc = adapter.transform(source_doc)
        except ConformanceError as e:
            errors += 1
            print(f"  SKIP {source_doc.ref.olf_id}: conformance: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001 - one bad act must not kill the run
            errors += 1
            print(f"  FAIL {source_doc.ref.olf_id}: {e}", file=sys.stderr)
            return
        out = archive_path(archive_root, doc.ref.olf_id)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(doc.akn_xml)
        written += 1
        print(f"  ok   {doc.ref.olf_id}  ({doc.provenance.source_sha256[:12]})")

    if hasattr(adapter, "fetch_many"):
        # Batch path: all export jobs are submitted first; the server processes
        # them in parallel, and we collect each result as it finishes.
        # Wall-time ≈ slowest single job rather than O(N × per-job time).
        for source_doc in adapter.fetch_many(refs):
            _write_doc(source_doc)
    else:
        # Per-act fallback for adapters that do not implement fetch_many.
        for ref in refs:
            try:
                source_doc = adapter.fetch(ref)
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"  FAIL {ref.olf_id}: fetch: {e}", file=sys.stderr)
                continue
            _write_doc(source_doc)

    # skipped = refs that were discovered but never produced a SourceDocument
    # (submit failures or batch timeouts); errors = transform failures.
    skipped = total - written - errors
    print(f"done: {written} written, {errors} errors, {skipped} skipped (of {total})")
    # Return 1 only on systemic failure: if every act failed to produce output
    # and there was actually something to process, something is deeply wrong.
    # Per-act errors are reported but not fatal — one bad source act must not
    # block the whole daily run.
    return 1 if (written == 0 and total > 0) else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jurisdiction", required=True)
    ap.add_argument("--archive", required=True, type=Path)
    ap.add_argument("--since", help="ISO8601; omit with --backfill")
    ap.add_argument("--backfill", action="store_true")
    args = ap.parse_args()

    if args.backfill and args.since:
        ap.error("--backfill and --since are mutually exclusive")
    since = None if args.backfill else (
        datetime.fromisoformat(args.since) if args.since else None
    )
    return run(args.jurisdiction, args.archive, since)


if __name__ == "__main__":
    raise SystemExit(main())
