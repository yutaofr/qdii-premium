"""Run the acceptance regressions for the four repaired P2 findings.

uv run python reports/mvp/evidence/futures-basis-acceptance-probes-2026-09-18.py
All simulated data use pytest temporary directories; network tests use FakeFetcher.
The original defect-presence probes have been replaced by correct-behavior assertions.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RESEARCH = "tests/unit/test_futures_basis_research.py::"
TOOL = "tests/unit/test_futures_basis_tool.py::"
CASES = [
    RESEARCH + "test_bootstrap_counts_nonempty_sessions_for_each_horizon",
    RESEARCH + "test_bootstrap_does_not_silently_discard_empty_resamples",
    RESEARCH + "test_sparse_horizon_and_same_start_comparison_do_not_borrow_other_days",
    TOOL + "test_official_hash_failure_cannot_verify_a_close",
    TOOL + "test_official_evidence_exports_verified_source_provenance",
    TOOL + "test_invalid_official_source_is_excluded_with_reason",
    TOOL + "test_response_identity_must_match_request",
    TOOL + "test_network_reparse_survives_hour_and_date_rotation",
]

if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", *CASES], cwd=REPO))
