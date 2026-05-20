"""
Thin wrapper around src.detect_payment_stack for use within the enrichment package.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.detect_payment_stack import detect_payment_stack


def detect(website_url: str | None) -> dict:
    """Detect payment/tech stack from a website URL.

    Delegates to src.detect_payment_stack.detect_payment_stack.
    """
    return detect_payment_stack(website_url)
