import requests
import structlog
from urllib3.util import Retry

from leetnotes_core.leetcode.settings import leetcode_settings

from .rate_limiter import JitteredLimiterAdapter

logger = structlog.get_logger(__name__)


def create_session(settings=leetcode_settings) -> requests.Session:
    """Create and configure the HTTP session used by LeetCodeAPI."""
    session = requests.Session()

    retries = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )

    rate_limiter = JitteredLimiterAdapter(
        per_second=settings.REQUESTS_PER_SECOND,
        max_retries=retries,
    )

    session.mount("https://", rate_limiter)
    session.mount("http://", rate_limiter)

    session.headers.update({
        "X-CSRFToken": settings.CSRF_TOKEN,
        "Content-Type": "application/json",
        "Referer": str(settings.BASE_URL),
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    })

    session.cookies.set(
        "LEETCODE_SESSION",
        settings.SESSION,
        domain="leetcode.com",
    )
    session.cookies.set(
        "csrftoken",
        settings.CSRF_TOKEN,
        domain="leetcode.com",
    )

    logger.info(
        "http_session_configured",
        requests_per_second=settings.REQUESTS_PER_SECOND,
        jitter_range=rate_limiter.jitter_range,
        max_retries=retries.total,
    )

    return session
