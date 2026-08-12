"""Import the repository-wide performance contract from Django code."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.perf.contract import (  # noqa: E402,F401
    API_MEASURED_RUNS,
    API_WARMUP_RUNS,
    DATASETS,
    DEEP_DASHBOARD_PAGE,
    REQUIRED_DATABASE_VENDOR,
    has_query_scaling_regression,
    summarize_samples,
)
