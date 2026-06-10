"""Italy adapter (Normattiva). Reference implementation of the Adapter protocol.

Italy is the "lucky" jurisdiction: the official source already emits Akoma Ntoso
and speaks NIR URNs, so this adapter mostly enumerates, fetches, validates, and
applies the AKN4OLF layer. It does not parse legal text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from ..base import Adapter, ActRef, SourceDocument, OLFDocument
from . import olf_layer
from .client import NormattivaClient, AKN_MEDIA_TYPE


class ItalyAdapter:
    name = "normattiva-it"
    version = "0.2.0"        # bump to force regeneration of all IT documents
    jurisdiction = "it"

    def __init__(self, client: NormattivaClient | None = None):
        self.client = client or NormattivaClient()

    def discover(self, since: datetime | None) -> Iterator[ActRef]:
        yield from self.client.search_modified_since(since)

    def fetch(self, ref: ActRef) -> SourceDocument:
        raw, source_url = self.client.fetch_akn(ref)
        return SourceDocument(
            ref=ref,
            raw_bytes=raw,
            media_type=AKN_MEDIA_TYPE,
            source_url=source_url,
            fetched_at=datetime.now(timezone.utc),
        )

    def fetch_many(self, refs: list[ActRef]) -> Iterator[SourceDocument]:
        """Batch path: submit all export jobs first, yield SourceDocuments as
        each one completes.  Wall-time ≈ slowest single job rather than
        O(N × per-job time).  Falls back gracefully: refs that fail to submit
        or time out are logged and skipped (they will be retried via the
        incremental overlap window on the next run).
        """
        for ref, raw_bytes, source_url in self.client.export_batch(refs):
            yield SourceDocument(
                ref=ref,
                raw_bytes=raw_bytes,
                media_type=AKN_MEDIA_TYPE,
                source_url=source_url,
                fetched_at=datetime.now(timezone.utc),
            )

    def transform(self, doc: SourceDocument) -> OLFDocument:
        return olf_layer.build(
            doc, adapter_name=self.name, adapter_version=self.version
        )


# Lets the runner load adapters generically by jurisdiction code.
def get_adapter() -> Adapter:
    return ItalyAdapter()
