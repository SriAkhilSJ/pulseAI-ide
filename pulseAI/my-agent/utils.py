"""Utility functions for price calculations."""
from constants import TAX_RATE


def calculate_total(price):
    """Apply tax to a price."""
    return price * (1 + TAX_RATE)
