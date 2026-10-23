import os
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
import logging

LOG_FILE = "data/compliance_audit.log"
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

USER_AGENT = "RivalIQ-Hackathon-Monitor/1.0 (Pune; compliance-checked)"

class ComplianceGuard:
    def __init__(self, rate_limit_seconds: float = 10.0):
        self.rate_limit_seconds = rate_limit_seconds
        self.domain_last_request: dict[str, float] = {}
        self.robot_parsers: dict[str, RobotFileParser] = {}

    def _get_domain(self, url: str) -> str:
        return urlparse(url).netloc

    def is_allowed_by_robots(self, url: str) -> bool:
        domain = self._get_domain(url)
        parsed_url = urlparse(url)
        robots_url = f"{parsed_url.scheme}://{domain}/robots.txt"

        if domain not in self.robot_parsers:
            rp = RobotFileParser()
            rp.set_url(robots_url)
            try:
                rp.read()
            except Exception:
                # Default to allowing if robots.txt is unreachable
                pass
            self.robot_parsers[domain] = rp

        allowed = self.robot_parsers[domain].can_fetch(USER_AGENT, url)
        status = "ALLOWED" if allowed else "DISALLOWED"
        logging.info(f"ROBOTS_CHECK | URL: {url} | User-Agent: {USER_AGENT} | Status: {status}")
        return allowed

    def enforce_rate_limit(self, url: str):
        domain = self._get_domain(url)
        now = time.time()
        if domain in self.domain_last_request:
            elapsed = now - self.domain_last_request[domain]
            if elapsed < self.rate_limit_seconds:
                sleep_time = self.rate_limit_seconds - elapsed
                logging.info(f"RATE_LIMIT | Domain: {domain} | Sleeping {sleep_time:.2f}s")
                time.sleep(sleep_time)

        self.domain_last_request[domain] = time.time()
        logging.info(f"FETCH_DISPATCH | URL: {url} | Domain: {domain}")

# Global compliance instance
compliance_guard = ComplianceGuard(rate_limit_seconds=2.0)