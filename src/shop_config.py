"""Shop settings loaded only from the hosting environment.

Set these variables in Vercel Project Settings > Environment Variables:
RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET, and SITE_URL.
Never put their values in source control or log secret values.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ShopConfig:
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str
    site_url: str

    @property
    def razorpay_configured(self):
        return bool(self.razorpay_key_id and self.razorpay_key_secret
                    and self.razorpay_webhook_secret)


def load_shop_config():
    """Read the current shop configuration without caching secret values."""
    return ShopConfig(
        razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID", "").strip(),
        razorpay_key_secret=os.environ.get("RAZORPAY_KEY_SECRET", "").strip(),
        razorpay_webhook_secret=os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip(),
        site_url=os.environ.get("SITE_URL", "").strip().rstrip("/"),
    )
