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
import time
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator, TypeVar

from adapters.base import Adapter, ActRef, ConformanceError

# Number of ActRefs to pull from discover() at a time before fetching/writing.
# Keeps in-flight work bounded; if the run is killed, all prior chunks are
# already committed+pushed and nothing is lost.
_RUN_CHUNK_SIZE = 25

# Backfill resume ledger: one codiceRedazionale per archived act, under the
# archive root. Surviving a crash, it lets a re-run skip everything already done.
_LEDGER_DIR = ".backfill"
_LEDGER_FILE = "done-codici.txt"

# No-progress stop: if this many consecutive chunks add ZERO newly-written acts,
# the export throttle has hit and further work this run is futile — stop cleanly.
_NO_PROGRESS_CHUNKS = 3

# Soft deadline (seconds) for a single backfill run, kept under the 24h Actions
# job cap. Overridable via --max-runtime-seconds.
_DEFAULT_MAX_RUNTIME_SECONDS = 72000  # 20h

_T = TypeVar("_T")


def load_adapter(jurisdiction: str) -> Adapter:
    mod = importlib.import_module(f"adapters.{jurisdiction}.adapter")
    return mod.get_adapter()


def archive_path(archive_root: Path, olf_id: str) -> Path:
    # olf:it/legge/2019/123  ->  it/legge/2019/123.akn.xml
    body = olf_id.split(":", 1)[1]
    return archive_root / (body + ".akn.xml")


def _ref_label(ref: ActRef) -> str:
    """A log-friendly label for an ActRef whose canonical olf_id may not yet be
    derived (it is filled in from the AKN during transform). Falls back to the
    source coordinates that uniquely identify the act before transform."""
    if ref.olf_id:
        return ref.olf_id
    parts = [p for p in (ref.denominazione, ref.anno, ref.numero) if p]
    label = " ".join(parts) if parts else "<unknown act>"
    if ref.codice_redazionale:
        label += f" [{ref.codice_redazionale}]"
    return label


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


def _ledger_path(archive_root: Path) -> Path:
    """Path to the backfill resume ledger under the archive root."""
    return archive_root / _LEDGER_DIR / _LEDGER_FILE


def _load_ledger(archive_root: Path) -> set[str]:
    """Load the set of already-archived codiceRedazionale values.

    Creates the ``.backfill/`` dir (and an empty ledger file) if absent so the
    first backfill run starts from a clean, committable state. One codice per
    line; blank lines ignored.
    """
    path = _ledger_path(archive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.strip()
        if code:
            done.add(code)
    return done


def _write_ledger(archive_root: Path, done: set[str]) -> None:
    """Rewrite the ledger file sorted + unique (one codice per line)."""
    path = _ledger_path(archive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(sorted(done))
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def run(
    jurisdiction: str,
    archive_root: Path,
    since: datetime | None,
    *,
    commit: bool = False,
    push: bool = False,
    git_name: str | None = None,
    git_email: str | None = None,
    max_runtime_seconds: float = _DEFAULT_MAX_RUNTIME_SECONDS,
    status_file: Path | None = None,
) -> int:
    adapter = load_adapter(jurisdiction)
    print(f"adapter {adapter.name}@{adapter.version} | since={since or 'BACKFILL'}")

    # The resume ledger and self-stop logic apply ONLY to backfill (since is
    # None). Incremental runs keep their original behaviour untouched (they rely
    # on the overlap window + idempotent transform, not on a ledger).
    is_backfill = since is None
    done_codici: set[str] = _load_ledger(archive_root) if is_backfill else set()
    done_before = len(done_codici)
    discovered_codici: set[str] = set()  # every codice seen this run (for `remaining`)

    written = errors = committed = pushed = processed = 0
    skipped_as_done = 0
    chunk_num = 0
    empty_streak = 0  # consecutive chunks that wrote 0 new acts (no-progress stop)
    stop_reason: str | None = None
    start = time.monotonic()

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
            _push(olf_id)

    def _push(label: str) -> None:
        """Push the current branch, retrying once on a transient failure. Shared
        by act commits and the per-chunk ledger commit."""
        nonlocal pushed
        for attempt in range(2):
            push_result = _git(archive_root, "push", check=False)
            if push_result.returncode == 0:
                pushed += 1
                return
            if attempt == 0:
                # Retry once on transient failure.
                continue
            # Second failure: warn and continue — the next push will carry
            # this commit along.
            print(
                f"  WARN push failed for {label}: {push_result.stderr.strip()}",
                file=sys.stderr,
            )

    def _commit_ledger(added: int) -> None:
        """Rewrite + commit (and push) the resume ledger for this chunk.

        Writes the sorted-unique ledger, stages it, and — if its content changed —
        commits it with the same git identity/push path as the act commits, so
        backfill progress survives a crash. A no-op when nothing changed.
        """
        _write_ledger(archive_root, done_codici)
        rel = _ledger_path(archive_root).relative_to(archive_root)
        _git(archive_root, "add", "--", str(rel))
        result = _git(archive_root, "diff", "--cached", "--quiet", check=False)
        if result.returncode == 0:
            return  # ledger unchanged — nothing to commit
        commit_cmd: list[str] = []
        if git_name:
            commit_cmd += ["-c", f"user.name={git_name}"]
        if git_email:
            commit_cmd += ["-c", f"user.email={git_email}"]
        commit_cmd += ["commit", "-m", f"backfill: ledger +{added}"]
        _git(archive_root, *commit_cmd)
        nonlocal committed
        committed += 1
        if push:
            _push("backfill ledger")

    def _write_doc(source_doc, written_codici: set[str]) -> None:
        """Transform one SourceDocument and write it; updates written/errors.

        On a successful write, the source ref's ``codice_redazionale`` (if any) is
        recorded in ``written_codici`` so the caller can add it to the resume
        ledger after the chunk's commits land.
        """
        nonlocal written, errors
        try:
            doc = adapter.transform(source_doc)
        except ConformanceError as e:
            errors += 1
            print(f"  SKIP {_ref_label(source_doc.ref)}: conformance: {e}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001 - one bad act must not kill the run
            errors += 1
            print(f"  FAIL {_ref_label(source_doc.ref)}: {e}", file=sys.stderr)
            return
        out = archive_path(archive_root, doc.ref.olf_id)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(doc.akn_xml)
        written += 1
        print(f"  ok   {doc.ref.olf_id}  ({doc.provenance.source_sha256[:12]})")
        if commit:
            _commit_doc(out, doc.ref.olf_id)
        # Record the codice so the chunk can persist it in the resume ledger. The
        # codice lives on the source ref (set at discovery), not the derived doc
        # ref — read it from the SourceDocument we were handed.
        codice = getattr(source_doc.ref, "codice_redazionale", None)
        if codice:
            written_codici.add(codice)

    def _process_chunk(chunk: list[ActRef], written_codici: set[str]) -> None:
        """Fetch and write a single chunk of ActRefs."""
        nonlocal errors
        if hasattr(adapter, "fetch_many"):
            # Batch path: submit all export jobs up-front; collect as each finishes.
            for source_doc in adapter.fetch_many(chunk):
                _write_doc(source_doc, written_codici)
        else:
            # Per-act fallback for adapters that do not implement fetch_many.
            for ref in chunk:
                try:
                    source_doc = adapter.fetch(ref)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    print(f"  FAIL {_ref_label(ref)}: fetch: {e}", file=sys.stderr)
                    continue
                _write_doc(source_doc, written_codici)

    # Stream discover() in chunks so backfill builds incrementally and commits
    # per-chunk rather than materialising the whole corpus before writing anything.
    for chunk in _chunked(adapter.discover(since), _RUN_CHUNK_SIZE):
        chunk_num += 1

        # Resume: for backfill, record every codice we discover (so we can compute
        # `remaining` honestly) and drop the ones already archived BEFORE fetching.
        if is_backfill:
            for ref in chunk:
                if ref.codice_redazionale:
                    discovered_codici.add(ref.codice_redazionale)
            todo = [
                ref for ref in chunk
                if not (ref.codice_redazionale and ref.codice_redazionale in done_codici)
            ]
            chunk_skipped = len(chunk) - len(todo)
            skipped_as_done += chunk_skipped
            if chunk_skipped:
                print(
                    f"chunk {chunk_num}: {chunk_skipped} already done (skipped before fetch)"
                )
        else:
            todo = list(chunk)
            chunk_skipped = 0

        chunk_written_before = written
        processed += len(todo)
        written_codici: set[str] = set()
        if todo:
            _process_chunk(todo, written_codici)
        chunk_written = written - chunk_written_before
        print(
            f"chunk {chunk_num}: {chunk_written} written, {errors} errors total"
            f" | total written={written} processed={processed}"
        )

        # Persist progress to the resume ledger every chunk (backfill only) so a
        # crash mid-run still leaves forward progress committed + pushed.
        if is_backfill and written_codici:
            done_codici |= written_codici
            _commit_ledger(len(written_codici))

        # --- single-run stop conditions (backfill only) -------------------
        if is_backfill:
            # No-progress / throttle stop: K consecutive chunks that ATTEMPTED
            # work (had un-done refs to fetch) yet wrote 0 acts. A chunk that was
            # entirely already-done (todo empty) is NOT a no-progress signal — it
            # is the resume fast-forwarding through ledgered work — so it neither
            # increments nor resets the streak.
            if todo:
                if chunk_written == 0:
                    empty_streak += 1
                else:
                    empty_streak = 0
            if empty_streak >= _NO_PROGRESS_CHUNKS:
                stop_reason = (
                    f"no progress for {empty_streak} consecutive chunks "
                    f"(export throttle hit); stopping cleanly"
                )
                print(f"stop: {stop_reason}")
                break
            # Soft deadline: stop before the 24h Actions job cap.
            elapsed = time.monotonic() - start
            if elapsed >= max_runtime_seconds:
                stop_reason = (
                    f"soft deadline reached ({elapsed:.0f}s >= "
                    f"{max_runtime_seconds:.0f}s); stopping cleanly"
                )
                print(f"stop: {stop_reason}")
                break

    # skipped = refs that were FETCHED but never produced a SourceDocument
    # (submit failures or batch timeouts); errors = transform failures.
    skipped = processed - written - errors
    summary = f"done: {written} written, {errors} errors, {skipped} skipped (of {processed})"
    if is_backfill:
        summary += f", {skipped_as_done} already-done"
    if commit:
        summary += f", {committed} committed"
    if push:
        summary += f", {pushed} pushed"
    print(summary)

    # --- completion reporting (backfill) ------------------------------------
    if is_backfill:
        # remaining = discovered codici not yet in the (updated) ledger. Acts with
        # no codice can't be ledgered/deduped, so they never count as "done"; they
        # are excluded from the denominator too (we only track what we can resume).
        remaining_set = discovered_codici - done_codici
        remaining = len(remaining_set)
        discovered = len(discovered_codici)
        complete = remaining == 0
        print(
            f"discovered={discovered} done_before={done_before} "
            f"written_this_run={written} remaining={remaining}"
        )
        print(f"BACKFILL_COMPLETE={'true' if complete else 'false'}")
        print(f"BACKFILL_PROGRESS={written}")
        if stop_reason:
            print(f"BACKFILL_STOP_REASON={stop_reason}")
        if status_file is not None:
            status_file.write_text(
                f"discovered={discovered} done_before={done_before} "
                f"written_this_run={written} remaining={remaining}\n"
                f"BACKFILL_COMPLETE={'true' if complete else 'false'}\n"
                f"BACKFILL_PROGRESS={written}\n",
                encoding="utf-8",
            )

    # Return 1 only on systemic failure: if every act failed to produce output
    # and there was actually something to process, something is deeply wrong.
    # Per-act errors are reported but not fatal — one bad source act must not
    # block the whole daily run. A backfill that wrote nothing because everything
    # was ALREADY DONE (or was throttled) is NOT a failure — it exits 0.
    if written == 0 and processed > 0 and not is_backfill:
        return 1
    return 0


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
    ap.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=_DEFAULT_MAX_RUNTIME_SECONDS,
        help="backfill soft deadline; stop cleanly after this many seconds "
        f"(default {_DEFAULT_MAX_RUNTIME_SECONDS}, ~20h, under the 24h job cap)",
    )
    ap.add_argument(
        "--status-file",
        type=Path,
        help="write the backfill completion status (discovered/done/written/"
        "remaining + BACKFILL_COMPLETE/BACKFILL_PROGRESS) to this path",
    )
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
        max_runtime_seconds=args.max_runtime_seconds,
        status_file=args.status_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
