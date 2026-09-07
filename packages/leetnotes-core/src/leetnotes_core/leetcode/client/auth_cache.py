import json

import structlog

from leetnotes_core.leetcode.settings import leetcode_settings

logger = structlog.get_logger(__name__)


def load_auth_cache() -> dict | None:
    """Load the cached authentication result, if available."""
    path = leetcode_settings.AUTH_CACHE_PATH

    if not path.exists():
        return None

    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("auth_cache_read_failed", path=str(path))
        return None


def save_auth_cache(credential_hash: str, verified_at: float) -> None:
    """Save a successful authentication check atomically."""
    path = leetcode_settings.AUTH_CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps({
            "credential_hash": credential_hash,
            "verified_at": verified_at,
        })
    )
    tmp_path.replace(path)

    logger.info("auth_cache_saved", path=str(path))


def clear_auth_cache() -> None:
    """Remove the cached authentication result."""
    path = leetcode_settings.AUTH_CACHE_PATH

    if path.exists():
        path.unlink()
        logger.info("auth_cache_cleared", path=str(path))
