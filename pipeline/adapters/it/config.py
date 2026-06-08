"""Typed loader for the Italy adapter's config.yaml.

Single place that reads the YAML. The code's job is to turn declarative knobs
into typed values; if a knob is missing we fall back to a sane default rather
than crashing, so a trimmed config still runs.

Deliberately NOT in here: secrets (Actions secrets) and the NIR->OLF type map
(urn.py) and the adapter version (adapter.py). See config.yaml header.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class SourceConfig:
    base_url: str = "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1"
    urn_resolver: str = "https://www.normattiva.it/uri-res/N2Ls"
    rate_limit_seconds: float = 1.0
    request_timeout_seconds: float = 30.0
    page_size: int = 200
    # Async AKN-export polling knobs (the export can take minutes). Optional;
    # defaults are sane for a public institutional API.
    export_poll_seconds: float = 5.0
    export_max_wait_seconds: float = 600.0
    multivigente: bool = True


@dataclass(frozen=True)
class IncrementalConfig:
    overlap_days: int = 2


@dataclass(frozen=True)
class OutputConfig:
    layout: str = "{jurisdiction}/{type}/{year}/{number}.akn.xml"
    encoding: str = "UTF-8"


@dataclass(frozen=True)
class DiffConfig:
    drop_change_classes: tuple[str, ...] = ("cosmetic",)
    always_commit_classes: tuple[str, ...] = (
        "text_amended",
        "vigenza_changed",
        "reference_changed",
    )


@dataclass(frozen=True)
class Config:
    source: SourceConfig
    incremental: IncrementalConfig
    output: OutputConfig
    diff: DiffConfig


@lru_cache(maxsize=1)
def load(path: Path | None = None) -> Config:
    p = path or _CONFIG_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    raw = raw or {}

    src = raw.get("source", {})
    inc = raw.get("incremental", {})
    out = raw.get("output", {})
    dif = raw.get("diff", {})

    return Config(
        source=SourceConfig(
            base_url=src.get("base_url", SourceConfig.base_url),
            urn_resolver=src.get("urn_resolver", SourceConfig.urn_resolver),
            rate_limit_seconds=float(src.get("rate_limit_seconds", SourceConfig.rate_limit_seconds)),
            request_timeout_seconds=float(src.get("request_timeout_seconds", SourceConfig.request_timeout_seconds)),
            page_size=int(src.get("page_size", SourceConfig.page_size)),
            export_poll_seconds=float(src.get("export_poll_seconds", SourceConfig.export_poll_seconds)),
            export_max_wait_seconds=float(src.get("export_max_wait_seconds", SourceConfig.export_max_wait_seconds)),
            multivigente=bool(src.get("multivigente", SourceConfig.multivigente)),
        ),
        incremental=IncrementalConfig(
            overlap_days=int(inc.get("overlap_days", IncrementalConfig.overlap_days)),
        ),
        output=OutputConfig(
            layout=out.get("layout", OutputConfig.layout),
            encoding=out.get("encoding", OutputConfig.encoding),
        ),
        diff=DiffConfig(
            drop_change_classes=tuple(dif.get("drop_change_classes", DiffConfig.drop_change_classes)),
            always_commit_classes=tuple(dif.get("always_commit_classes", DiffConfig.always_commit_classes)),
        ),
    )
