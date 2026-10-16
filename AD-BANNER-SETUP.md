# Trimzee third-party banner ads

The homepage contains a responsive ad placement labelled **ADVERTISEMENT**.

## Current behavior

- With no ad settings, visitors see a clearly labelled placeholder; no ad network is contacted.
- A sponsor banner can be supplied through the homepage element's `data-sponsor-image`, `data-sponsor-url` and optional `data-sponsor-alt` attributes.
- Google AdSense rendering is enabled only after valid publisher and ad-slot IDs are provided through `data-adsense-client` and `data-adsense-slot`.
- Ad URLs are limited to HTTP(S), sponsor links use `rel="sponsored noopener noreferrer"`, and ad load failures do not break the booking UI.
- CSS is responsive for desktop and mobile.

## Before serving real ads

1. Create/verify the publisher account and site using the ad network's official onboarding.
2. Replace the empty attributes on `data-tz-ad-slot` in `src/templates/index.html` with the actual publisher/client ID and slot ID, or configure a real sponsor image URL and destination.
3. Review the provider's consent, privacy, eligibility and site approval requirements before enabling ads.
4. Test the preview on desktop and mobile. Do not count the placeholder as revenue-generating inventory.

This branch intentionally does not include real publisher IDs or third-party tracking until the owner configures an approved advertising account.
