import hashlib
import time
from typing import ClassVar

import requests
import structlog

from leetnotes_core.leetcode.auth_cache import (
    clear_auth_cache,
    load_auth_cache,
    save_auth_cache,
)
from leetnotes_core.leetcode.settings import leetcode_settings

logger = structlog.get_logger(__name__)


class LeetCodeAuthenticationError(RuntimeError):
    """Raised when LEETCODE_SESSION/LEETCODE_CSRF_TOKEN look invalid or
    expired — see LeetCodeClient.ensure_authenticated. LeetCode's GraphQL API
    doesn't reject a bad session with an HTTP error or a GraphQL 'errors'
    entry; user-scoped fields (submissions, solved list, ...) just silently
    resolve to null/empty instead, which would otherwise only surface much
    later as a confusing crash deep in parsing code (e.g. len(None))."""


class LeetCodeAuthValidator:
    """Handles authentication validation for LeetCode API requests."""

    _AUTH_ERROR_MESSAGE: ClassVar[str] = (
        "LeetCode says you're not signed in — LEETCODE_SESSION/LEETCODE_CSRF_TOKEN "
        "in .env look invalid or expired. Copy fresh values from an authenticated "
        "browser session and try again."
    )

    def __init__(self, settings=leetcode_settings):
        self.settings = settings
        self._authenticated: bool | None = None

    def _credential_hash(self) -> str:
        """One-way hash of the current SESSION+CSRF_TOKEN — identifies
        *which* credentials a cached authentication result belongs to,
        without ever writing the raw secrets to disk."""
        raw = f"{self.settings.SESSION}:{self.settings.CSRF_TOKEN}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _probe_signed_in(self, session: requests.Session) -> bool:
        """The actual network call: asks LeetCode directly whether the
        current session is signed in."""
        graphql_url = f"{self.settings.BASE_URL}/graphql"

        query = "query globalData { userStatus { isSignedIn } }"
        try:
            response = session.post(graphql_url, json={"query": query})
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.RequestException:
            logger.exception("authentication_check_failed")
            raise
        return bool(
            ((result.get("data") or {}).get("userStatus") or {}).get("isSignedIn")
        )

    def ensure_authenticated(self, session: requests.Session, force: bool = False) -> None:
        """Verifies LEETCODE_SESSION/CSRF_TOKEN are actually valid before a
        user-scoped request goes out.

        Three layers of caching, cheapest first (skipped entirely when
        force=True — used for a reactive re-check after an already-suspicious
        result, see get_solved_questions/get_recent_ac_submissions/
        LeetCodeSyncManager.populate_submission_code):
        1. In-memory, per client instance — a batch run only ever pays for
           one real check no matter how many slugs it processes.
        2. On-disk (see auth_cache.py) — a fresh CLI process (a new client
           instance) skips the check too, as long as SESSION/CSRF_TOKEN
           haven't changed (credential hash still matches) and the cached
           result isn't older than AUTH_CHECK_TTL_SECONDS. The TTL exists
           because a session can go stale on LeetCode's side without the
           .env values themselves ever changing — trusting a hash match
           forever would silently miss exactly that case.
        3. Otherwise: an actual network check, whose result updates both
           caches — success refreshes the on-disk record (resets the TTL
           clock); failure clears it, so a later, separate CLI invocation
           within the old TTL window doesn't keep trusting a now-known-stale
           record either.

        Raises LeetCodeAuthenticationError if LeetCode says we're not signed in.
        """
        if not force:
            if self._authenticated is not None:
                if not self._authenticated:
                    raise LeetCodeAuthenticationError(self._AUTH_ERROR_MESSAGE)
                return

            cached = load_auth_cache()
            if cached and cached.get("credential_hash") == self._credential_hash():
                age = time.time() - cached.get("verified_at", 0)
                if age < self.settings.AUTH_CHECK_TTL_SECONDS:
                    self._authenticated = True
                    logger.info(
                        "authentication_check_skipped",
                        reason="cached_and_fresh",
                        age_seconds=round(age),
                    )
                    return

        signed_in = self._probe_signed_in(session)
        self._authenticated = signed_in
        if signed_in:
            save_auth_cache(self._credential_hash(), time.time())
        else:
            clear_auth_cache()
        logger.info("authentication_check_completed", signed_in=signed_in, forced=force)
        if not signed_in:
            raise LeetCodeAuthenticationError(self._AUTH_ERROR_MESSAGE)
