import re

BOT_WALL_PATTERNS = [
    r"Cloudflare",
    r"Verify you are human",
    r"Access Denied",
    r"Attention Required! \| Cloudflare",
    r"Please enable Cookies",
    r"security check to access"
]

class CaptureQualityGuard:
    @staticmethod
    def inspect_capture(raw_text: str, http_status: int = 200, resolved_url: str = "") -> tuple[bool, str | None]:
        # 1. HTTP Status Check
        if http_status in (403, 429, 503):
            return False, f"HTTP_BLOCK_{http_status}"

        # 2. Empty DOM Check
        if len(raw_text.strip()) < 150:
            return False, "EMPTY_DOM"

        # 3. Bot Wall / CAPTCHA Check
        for pattern in BOT_WALL_PATTERNS:
            if re.search(pattern, raw_text, re.IGNORECASE):
                return False, f"BOT_WALL_DETECTED: {pattern}"

        # 4. Consent Interstitial Blocking Check
        # If page contains massive cookie overlay strings with minimal core text content
        if "Accept All Cookies" in raw_text and len(raw_text.strip()) < 250:
            return False, "CONSENT_INTERSTITIAL_BLOCK"

        # 5. Region Redirect Check
        # Check if region switched away from expected defaults (e.g. redirected to USD / global portal)
        if "$ " in raw_text and "₹" not in raw_text:
            return False, "REGION_REDIRECT_NON_INR"

        return True, None