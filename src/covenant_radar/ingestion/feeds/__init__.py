"""External feed ingestion: the `C-56` `FeedAdapter` framework and sources.

`framework.py` polls every configured adapter through one watermark and
retention policy, isolating a failing or unconfigured feed so it degrades
alone (`spec §R-30.d`). `synthetic.py` is the documented default source
(`spec §12.1` [OPEN-05]) until a licensed subscription exists.
"""

from __future__ import annotations

from covenant_radar.ingestion.feeds.framework import (
    DEFAULT_RETENTION_DAYS,
    FeedPollFramework,
    FeedPollReport,
    FeedSourceOutcome,
    FeedWatermarkStore,
    InMemoryFeedWatermarkStore,
)
from covenant_radar.ingestion.feeds.synthetic import SyntheticFeedAdapter, SyntheticFeedConfig

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "FeedPollFramework",
    "FeedPollReport",
    "FeedSourceOutcome",
    "FeedWatermarkStore",
    "InMemoryFeedWatermarkStore",
    "SyntheticFeedAdapter",
    "SyntheticFeedConfig",
]
