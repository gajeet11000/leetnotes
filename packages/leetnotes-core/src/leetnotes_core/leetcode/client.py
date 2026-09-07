import structlog
from typing import ClassVar

from leetnotes_core.leetcode.auth_validator import LeetCodeAuthValidator, LeetCodeAuthenticationError
from leetnotes_core.leetcode.session_manager import LeetCodeSessionManager
from leetnotes_core.leetcode.settings import leetcode_settings

logger = structlog.get_logger(__name__)


class LeetCodeClient:
    _DIFFICULTY_LEVELS: ClassVar[dict[int, str]] = {
        0: "Unknown",
        1: "Easy",
        2: "Medium",
        3: "Hard",
    }

    def __init__(self, settings=leetcode_settings):
        self.settings = settings
        self.session_manager = LeetCodeSessionManager(settings)
        self.auth_validator = LeetCodeAuthValidator(settings)
        self.graphql_url = f"{self.settings.BASE_URL}/graphql"
        self.session = self.session_manager.get_session()

    def get_solved_questions(self) -> list[dict]:
        """Fetches all solved problems ('ac' status) from the REST API endpoint.

        The same response already carries id/title/difficulty for every
        problem (not just slug), at no extra request cost — pulling those
        out here lets callers (e.g. the CLI picker) label a slug that's
        solved on LeetCode but not yet fetched locally, without a per-slug
        GraphQL round trip.
        """
        self.auth_validator.ensure_authenticated(self.session)

        url = self.settings.ENDPOINT_ALL_PROBLEMS
        logger.info("solved_questions_request_started", url=url)

        try:
            response = self.session.get(url)
            response.raise_for_status()
        except Exception:
            logger.exception("solved_questions_request_failed", url=url)
            raise

        data = response.json()
        raw_pairs = data.get("stat_status_pairs", [])

        solved_problems = []

        for pair in raw_pairs:
            if pair.get("status") != "ac":
                continue
            stat = pair.get("stat") or {}
            solved_problems.append(
                {
                    "slug": stat.get("question__title_slug"),
                    "id": stat.get("frontend_question_id"),
                    "title": stat.get("question__title"),
                    "difficulty": self._DIFFICULTY_LEVELS.get(
                        (pair.get("difficulty") or {}).get("level", 0)
                    ),
                }
            )

        if not solved_problems:
            # Coming back with zero solved problems is essentially always a
            # dead session rather than reality (see LeetCodeSyncManager —
            # this endpoint is the source of truth for "solved" in the first
            # place). Force a live, uncached re-check to get a definitive
            # answer rather than silently trusting "zero solved" — a
            # genuinely brand-new account still comes through fine, since
            # this only raises if LeetCode itself confirms we're logged out.
            logger.warning(
                "solved_questions_empty_result", action="forcing_auth_recheck"
            )
            self.auth_validator.ensure_authenticated(self.session, force=True)

        logger.info(
            "solved_questions_request_succeeded",
            solved_count=len(solved_problems),
        )
        return solved_problems

    def get_question_details(self, slug: str) -> dict:
        """Queries LeetCode's GraphQL API to get comprehensive metadata for a specific problem."""
        log = logger.bind(slug=slug)
        query = """
        query selectQuestion($titleSlug: String!) {
          question(titleSlug: $titleSlug) {
            content
            questionFrontendId
            title
            titleSlug
            difficulty
            categoryTitle
            topicTags {
              name
              slug
            }
          }
        }
        """

        payload = {"query": query, "variables": {"titleSlug": slug}}

        log.info("question_details_request_started")
        try:
            response = self.session.post(self.graphql_url, json=payload)
            response.raise_for_status()
        except Exception:
            log.exception("question_details_request_failed")
            raise

        result = response.json()
        if "errors" in result:
            log.error("question_details_request_failed", errors=result["errors"])
            raise RuntimeError(f"GraphQL Error: {result['errors']}")

        question = (result.get("data") or {}).get("question")
        if not question:
            log.warning("question_details_not_found")
        else:
            log.info("question_details_request_succeeded", title=question.get("title"))

        return result

    def get_submission_list(self, slug: str, limit: int = 20) -> dict:
        """Queries LeetCode GraphQL to retrieve the submission history for a given problem."""
        self.auth_validator.ensure_authenticated(self.session)
        log = logger.bind(slug=slug)
        query = """
        query submissionList($questionSlug: String!, $limit: Int, $offset: Int) {
          questionSubmissionList(
            questionSlug: $questionSlug
            limit: $limit
            offset: $offset
          ) {
            submissions {
              id
              statusDisplay
              lang
              timestamp
            }
          }
        }
        """

        payload = {
            "query": query,
            "variables": {
                "questionSlug": slug,
                "limit": limit,
                "offset": 0,
            },
        }

        log.info("submission_list_request_started", limit=limit)
        try:
            response = self.session.post(self.graphql_url, json=payload)
            response.raise_for_status()
        except Exception:
            log.exception("submission_list_request_failed")
            raise

        result = response.json()
        if "errors" in result:
            log.error("submission_list_request_failed", errors=result["errors"])
            raise RuntimeError(f"GraphQL Error: {result['errors']}")

        submissions = (
            (result.get("data") or {}).get("questionSubmissionList") or {}
        ).get("submissions") or []
        log.info("submission_list_request_succeeded", submission_count=len(submissions))

        return result

    def get_recent_ac_submissions(
        self, username: str | None = None, limit: int = 20
    ) -> dict:
        """Queries LeetCode GraphQL for a user's most recent accepted submissions.

        Uses `username` if given, otherwise falls back to `LEETCODE_USERNAME`
        from settings. Raises ValueError if neither is available.
        """
        self.auth_validator.ensure_authenticated(self.session)

        target_username = username or self.settings.USERNAME
        if not target_username:
            raise ValueError(
                "No LeetCode username available — pass one explicitly or set LEETCODE_USERNAME."
            )

        log = logger.bind(username=target_username)
        query = """
        query recentAcSubmissions($username: String!, $limit: Int!) {
          recentAcSubmissionList(username: $username, limit: $limit) {
            title
            titleSlug
            timestamp
          }
        }
        """

        payload = {
            "query": query,
            "variables": {"username": target_username, "limit": limit},
        }

        log.info("recent_ac_submissions_request_started", limit=limit)
        try:
            response = self.session.post(self.graphql_url, json=payload)
            response.raise_for_status()
        except Exception:
            log.exception("recent_ac_submissions_request_failed")
            raise

        result = response.json()
        if "errors" in result:
            log.error("recent_ac_submissions_request_failed", errors=result["errors"])
            raise RuntimeError(f"GraphQL Error: {result['errors']}")

        submissions = (result.get("data") or {}).get("recentAcSubmissionList") or []
        if not submissions:
            # Same reasoning as get_solved_questions: a live re-check costs
            # nothing when this is a genuinely quiet period (no false
            # positive — it only raises if LeetCode confirms we're logged
            # out), but catches a dead session that would otherwise look
            # identical to "nothing recent to report".
            log.warning(
                "recent_ac_submissions_empty_result", action="forcing_auth_recheck"
            )
            self.auth_validator.ensure_authenticated(self.session, force=True)
        log.info(
            "recent_ac_submissions_request_succeeded", submission_count=len(submissions)
        )

        return result

    def get_submission_details(self, submission_id: int) -> dict:
        """Queries LeetCode GraphQL to get full details and source code for a specific submission ID."""
        self.auth_validator.ensure_authenticated(self.session)
        log = logger.bind(submission_id=submission_id)
        query = """
        query submissionDetails($submissionId: Int!) {
          submissionDetails(submissionId: $submissionId) {
            id
            code
            timestamp
            statusCode
            lang {
              name
              verboseName
            }
            runtimeDisplay
            memoryDisplay
          }
        }
        """

        payload = {
            "query": query,
            "variables": {"submissionId": int(submission_id)},
        }

        log.info("submission_details_request_started")
        try:
            response = self.session.post(self.graphql_url, json=payload)
            response.raise_for_status()
        except Exception:
            log.exception("submission_details_request_failed")
            raise

        result = response.json()
        if "errors" in result:
            log.error("submission_details_request_failed", errors=result["errors"])
            raise RuntimeError(f"GraphQL Error: {result['errors']}")

        log.info("submission_details_request_succeeded")
        return result
