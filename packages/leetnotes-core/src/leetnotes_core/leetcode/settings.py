from pathlib import Path

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from leetnotes_core.config import BaseProjectSettings


class LeetCodeSettings(BaseProjectSettings):
    model_config = SettingsConfigDict(
        env_prefix="LEETCODE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    SESSION: str = Field(..., description="LeetCode session cookie")
    CSRF_TOKEN: str = Field(..., description="LeetCode CSRF token")

    # Optional (not a secret) — only needed for the recentAcSubmissionList
    # query, which takes a username rather than reading it off the session.
    USERNAME: str | None = Field(default=None, description="LeetCode public username")

    BASE_URL: str = "https://leetcode.com"

    ENDPOINT_ALL_PROBLEMS: str = f"{BASE_URL}/api/problems/algorithms/"

    # Kept conservative on purpose — a large batch run (e.g. populating hundreds
    # of solved problems in one sitting) is exactly the traffic shape abuse
    # detection looks for. See modules/leetcode/rate_limiting.py.
    REQUESTS_PER_SECOND: float = 1.0

    DATA_STORAGE_DIR: Path = BaseProjectSettings.PROJECT_ROOT_DIR / "LEETCODE_DATA"
    PROBLEMS_DATA_DIR: Path = DATA_STORAGE_DIR / "dsa_problems"

    # SQLite file backing problems/tags/pending_cache — community/public
    # data, safe to commit. See modules/leetcode/storage/db.py for the
    # schema and connection setup.
    DSA_DB_PATH: Path = PROBLEMS_DATA_DIR / "leetcode.db"

    # Separate SQLite file backing submissions only — personal solution
    # code, kept out of DSA_DB_PATH on purpose so that file can safely be
    # committed/shared without leaking anyone's solutions. Never commit
    # this file (see .gitignore).
    SUBMISSIONS_DB_PATH: Path = PROBLEMS_DATA_DIR / "submissions.db"

    DSA_PROBLEMS_ASSETS_DIR: Path = PROBLEMS_DATA_DIR / "assets"

    # Tiny local cache remembering the last time SESSION/CSRF_TOKEN were
    # confirmed valid (a hash of them, plus when — never the raw values), so
    # LeetCodeClient.ensure_authenticated doesn't need a network round trip
    # on every fresh CLI invocation. Personal machine state, not problem
    # data — never commit this file (see .gitignore). See auth_cache.py.
    AUTH_CACHE_PATH: Path = PROBLEMS_DATA_DIR / "auth_check_cache.json"

    # How long a cached "yes, this session works" result is trusted before
    # ensure_authenticated re-checks with LeetCode again. Deliberately short
    # rather than "forever" — a session that's gone stale without SESSION/
    # CSRF_TOKEN themselves changing would otherwise keep being trusted
    # indefinitely. See LeetCodeClient.ensure_authenticated.
    AUTH_CHECK_TTL_SECONDS: float = 3600.0


leetcode_settings = LeetCodeSettings()
