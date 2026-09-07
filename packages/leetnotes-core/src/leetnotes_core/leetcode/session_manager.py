import requests
import structlog
from urllib3.util import Retry

from leetnotes_core.leetcode.rate_limiting import JitteredLimiterAdapter
from leetnotes_core.leetcode.settings import leetcode_settings

logger = structlog.get_logger(__name__)


class LeetCodeSessionManager:
    """Manages the HTTP session configuration for LeetCode API requests."""

    def __init__(self, settings=leetcode_settings):
        self.settings = settings
        self.session = requests.Session()
        self._setup_session()

    def _setup_session(self):
        # 1. Automatic Retries on HTTP 429 (Too Many Requests) or Server Errors (5xx)
        retries = Retry(
            total=3,  # Total number of retries
            backoff_factor=2,  # Exponential backoff: 2s, 4s, 8s
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )

        # 2. Rate Limiting Adapter, plus jitter — see rate_limiting.py for why.
        rate_limiter = JitteredLimiterAdapter(
            per_second=self.settings.REQUESTS_PER_SECOND,
            max_retries=retries,
        )

        # Mount the rate limiter and retries on all HTTP/HTTPS endpoints
        self.session.mount("https://", rate_limiter)
        self.session.mount("http://", rate_limiter)

        logger.info(
            "http_session_configured",
            requests_per_second=self.settings.REQUESTS_PER_SECOND,
            jitter_range=rate_limiter.jitter_range,
            max_retries=retries.total,
        )

        # Configure Headers and Cookies
        self.session.headers.update(
            {
                "X-CSRFToken": self.settings.CSRF_TOKEN,
                "Content-Type": "application/json",
                "Referer": str(self.settings.BASE_URL),
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
        )
        self.session.cookies.set(
            "LEETCODE_SESSION",
            self.settings.SESSION,
            domain="leetcode.com",
        )
        self.session.cookies.set(
            "csrftoken",
            self.settings.CSRF_TOKEN,
            domain="leetcode.com",
        )

    def get_session(self) -> requests.Session:
        """Get the configured requests session."""
        return self.session