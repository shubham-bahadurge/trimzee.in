"""Business details and legal metadata re-exported from config.business."""
try:
    from config.business import BUSINESS, UPDATED
except ImportError:
    from src.config.business import BUSINESS, UPDATED

__all__ = ["BUSINESS", "UPDATED"]
