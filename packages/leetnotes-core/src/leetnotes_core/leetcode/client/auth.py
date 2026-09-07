import hashlib
import time
from typing import Any

import requests
import structlog

from leetnotes_core.leetcode.settings import leetcode_settings

from .auth_cache import clear_auth_cache, load_auth_cache, save_auth_cache

logger = structlog.get_logger(__name__)

_AUTH_ERROR_MESSAGE = (
    "LeetCode says you're not signed in — "
    "LEETCODE_SESSION/LEETCODE_CSRF_TOKEN in .env look invalid or expired. "
    "Copy fresh values from an authenticated browser session and try again."
)


class LeetCodeAuthenticationError(RuntimeError):
    """Raised when the configured LeetCode session is not authenticated."""


class Authenticator:
    """Validate and cache authentication state for a LeetCode session."""

    def __init__(self, settings=leetcode_settings):
        self.settings = settings
        self._authenticated: bool | None = None

    def ensure_authenticated(
        self,
        session: requests.Session,
        *,
        force: bool = False,
    ) -> None:
        """Raise if the current LeetCode credentials are not valid."""
        if not force:
            # Fastest path: this Authenticator already checked the session.
            if self._authenticated is not None:
                if not self._authenticated:
                    raise LeetCodeAuthenticationError(_AUTH_ERROR_MESSAGE)
                return

            # Second path: another CLI invocation may have recently
            # verified the same credentials and persisted the result.
            if self._cached_result_is_valid():
                self._authenticated = True
                return

        # Slow path: ask LeetCode directly.
        signed_in = self._probe(session)
        self._authenticated = signed_in

        if signed_in:
            save_auth_cache(self._credential_hash(), time.time())
        else:
            clear_auth_cache()

        logger.info(
            "authentication_check_completed",
            signed_in=signed_in,
            forced=force,
        )

        if not signed_in:
            raise LeetCodeAuthenticationError(_AUTH_ERROR_MESSAGE)

    def _cached_result_is_valid(self) -> bool:
        cached = load_auth_cache()

        if not cached:
            return False

        if cached.get("credential_hash") != self._credential_hash():
            return False

        age = time.time() - cached.get("verified_at", 0)

        if age >= self.settings.AUTH_CHECK_TTL_SECONDS:
            return False

        logger.info(
            "authentication_check_skipped",
            reason="cached_and_fresh",
            age_seconds=round(age),
        )
        return True

    def _credential_hash(self) -> str:
        """Identify credentials without storing the credentials themselves."""
        raw = f"{self.settings.SESSION}:{self.settings.CSRF_TOKEN}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _probe(self, session: requests.Session) -> bool:
        """Ask LeetCode whether the current session is signed in."""
        query = "query globalData { userStatus { isSignedIn } }"

        try:
            response = session.post(
                f"{self.settings.BASE_URL}/graphql",
                json={"query": query},
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
        except requests.exceptions.RequestException:
            logger.exception("authentication_check_failed")
            raise

        return bool(
            ((result.get("data") or {}).get("userStatus") or {}).get("isSignedIn")
        )
