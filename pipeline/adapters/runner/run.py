"""Generic runner. Loads a jurisdiction adapter, walks discover->fetch->transform,
and writes AKN4OLF documents into a local checkout of the `archive` repo.

By default it does NOT commit. Pass --commit to create one git commit per act
written (skipping acts whose bytes are identical to what is already committed),
and --push to push each commit to the archive remote immediately after it is made.
--push implies --commit.

Layout written into the archive:

    <archive>/it/<olf-type>/<year>/<number>.akn.xml

Usage (must be run from the inner `pipeline/` directory that contains `adapters/`):
    python -m adapters.runner.run --jurisdiction it --archive /path/to/archive [--since ISO8601]
    python -m adapters.runner.run --jurisdiction it --archive /path/to/archive --backfill
    python -m adapters.runner.run --jurisdiction it --archive /path/to/archive --since ISO8601 \\
        --commit --push --git-name "normattiva-adapter" \\
        --git-email "normattiva-adapter@users.noreply.github.com"
"""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator, TypeVar

from adapters.base import Adapter, ActRef, ConformanceError

# Number of ActRefs to pull from discover() at a time before fetching/writing.
# Keeps in-flight work bounded; if the run is killed, all prior chunks are
# already committed+pushed and nothing is lost.
_RUN_CHUNK_SIZE = 25

_T = TypeVar("_T")


def load_adapter(jurisdiction: str) -> Adapter:
    mod = importlib.import_module(f"adapters.{jurisdiction}.adapter")
    return mod.get_adapter()


def archive_path(archive_root: Path, olf_id: str) -> Path:
    # olf:it/legge/2019/123  ->  it/legge/2019/123.akn.xml
    body = olf_id.split(":", 1)[1]
    return archive_root / (body + ".akn.xml")


def _chunked(iterable: Iterable[_T], n: int) -> Iterator[list[_T]]:
    """Yield successive non-overlapping lists of up to *n* items from *iterable*
    without materialising the whole iterable up-front."""
    it = iter(iterable)
    while True:
        chunk = list(islice(it, n))
        if not chunk:
            return
        yield chunk


def _git(archive_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run `git -C <archive_root> <args>` and return the CompletedProcess."""
    return subprocess.run(
        ["git", "-C", str(archive_root), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def run(
    jurisdiction: str,
    archive_root: Path,
    since: datetime | None,
    *,
    commit: bool = False,
    push: bool = False,
    git_name: str | None = None,
    git_email: str | None = None,
) -> int:
    adapter = load_adapter(jurisdiction)
    print(f"adapter {adapter.name}@{adapter.version} | since={since or 'BACKFILL'}")

    written = errors = committed = pushed = processed = 0
    chunk_num = 0

    def _commit_doc(out: Path, olf_id: str) -> None:
        """Git-add the file and commit it if the content actually changed."""
        nonlocal committed, pushed

        # Stage the file using its path relative to the archive root.
        rel = out.relative_to(archive_root)
        _git(archive_root, "add", "--", str(rel))

        # Check whether anything is staged; returncode 0 means no diff → skip.
        result = _git(archive_root, "diff", "--cached", "--quiet", check=False)
        if result.returncode == 0:
            # Identical bytes already in the tree — idempotent, do not commit.
            return

        # Build commit command with optional identity overrides.
        commit_cmd: list[str] = []
        if git_name:
            commit_cmd += ["-c", f"user.name={git_name}"]
        if git_email:
            commit_cmd += ["-c", f"user.email={git_email}"]
        commit_cmd += ["commit", "-m", f"it: {olf_id}"]
        _git(archive_root, *commit_cmd)
        committed += 1

        if push:
            for attempt in range(2):
                push_result = _git(archive_root, "push", check=False)
                if push_result.returncode == 0:
                    pushed += 1
                    break
                if attempt == 0:
                    # Retry once on transient failure.
                    continue
                # Second failure: warn and continue — the next push will carry
                # this commit along.
                print(
                    f"  WARN push failed for {olf_id}: {push_result.stderr.strip()}",
                    file=sys.stderr,
                )

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
        if commit:
            _commit_doc(out, doc.ref.olf_id)

    def _process_chunk(chunk: list[ActRef]) -> None:
        """Fetch and write a single chunk of ActRefs."""
        nonlocal errors
        if hasattr(adapter, "fetch_many"):
            # Batch path: submit all export jobs up-front; collect as each finishes.
            for source_doc in adapter.fetch_many(chunk):
                _write_doc(source_doc)
        else:
            # Per-act fallback for adapters that do not implement fetch_many.
            for ref in chunk:
                try:
                    source_doc = adapter.fetch(ref)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    print(f"  FAIL {ref.olf_id}: fetch: {e}", file=sys.stderr)
                    continue
                _write_doc(source_doc)

    # Stream discover() in chunks so backfill builds incrementally and commits
    # per-chunk rather than materialising the whole corpus before writing anything.
    for chunk in _chunked(adapter.discover(since), _RUN_CHUNK_SIZE):
        chunk_num += 1
        chunk_written_before = written
        processed += len(chunk)
        _process_chunk(chunk)
        chunk_written = written - chunk_written_before
        print(
            f"chunk {chunk_num}: {chunk_written} written, {errors} errors total"
            f" | total written={written} processed={processed}"
        )

    # skipped = refs that were discovered but never produced a SourceDocument
    # (submit failures or batch timeouts); errors = transform failures.
    skipped = processed - written - errors
    summary = f"done: {written} written, {errors} errors, {skipped} skipped (of {processed})"
    if commit:
        summary += f", {committed} committed"
    if push:
        summary += f", {pushed} pushed"
    print(summary)
    # Return 1 only on systemic failure: if every act failed to produce output
    # and there was actually something to process, something is deeply wrong.
    # Per-act errors are reported but not fatal — one bad source act must not
    # block the whole daily run.
    return 1 if (written == 0 and processed > 0) else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jurisdiction", required=True)
    ap.add_argument("--archive", required=True, type=Path)
    ap.add_argument("--since", help="ISO8601; omit with --backfill")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument(
        "--commit",
        action="store_true",
        help="git-add + git-commit each act as it is written",
    )
    ap.add_argument(
        "--push",
        action="store_true",
        help="git push after each commit (implies --commit)",
    )
    ap.add_argument("--git-name", help="committer name for this run")
    ap.add_argument("--git-email", help="committer email for this run")
    args = ap.parse_args()

    if args.backfill and args.since:
        ap.error("--backfill and --since are mutually exclusive")

    do_commit = args.commit or args.push
    do_push = args.push

    since = None if args.backfill else (
        datetime.fromisoformat(args.since) if args.since else None
    )
    return run(
        args.jurisdiction,
        args.archive,
        since,
        commit=do_commit,
        push=do_push,
        git_name=args.git_name,
        git_email=args.git_email,
    )


if __name__ == "__main__":
    raise SystemExit(main())
