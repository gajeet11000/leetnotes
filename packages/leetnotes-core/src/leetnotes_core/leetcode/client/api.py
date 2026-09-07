from typing import Any

import requests
import structlog

from leetnotes_core.leetcode.settings import leetcode_settings

from .auth import Authenticator
from .graphql import execute
from .queries import (
    QUESTION_DETAILS_QUERY,
    RECENT_AC_SUBMISSIONS_QUERY,
    SUBMISSION_DETAILS_QUERY,
    SUBMISSION_LIST_QUERY,
)
from .session import create_session

logger = structlog.get_logger(__name__)

DIFFICULTY_LEVELS = {
    0: "Unknown",
    1: "Easy",
    2: "Medium",
    3: "Hard",
}


class LeetCodeAPIClient:
    """High-level interface to LeetCode's REST and GraphQL APIs."""

    def __init__(self, settings=leetcode_settings):
        self.settings = settings
        self.session = create_session(settings)
        self.auth = Authenticator(settings)
        self.graphql_url = f"{settings.BASE_URL}/graphql"

    def get_solved_questions(self) -> list[dict[str, Any]]:
        """Return all problems currently marked as accepted."""
        self.auth.ensure_authenticated(self.session)

        url = self.settings.ENDPOINT_ALL_PROBLEMS
        logger.info("solved_questions_request_started", url=url)

        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException:
            logger.exception("solved_questions_request_failed", url=url)
            raise

        solved = [
            self._parse_solved_problem(pair)
            for pair in data.get("stat_status_pairs", [])
            if pair.get("status") == "ac"
        ]

        # An unexpected empty user-scoped response can also be caused by
        # an expired session, so confirm authentication with a live check.
        if not solved:
            logger.warning(
                "solved_questions_empty_result",
                action="forcing_auth_recheck",
            )
            self.auth.ensure_authenticated(self.session, force=True)

        logger.info(
            "solved_questions_request_succeeded",
            solved_count=len(solved),
        )
        return solved

    def get_question_details(self, slug: str) -> dict[str, Any]:
        """Return complete metadata for a problem."""
        self.auth.ensure_authenticated(self.session)

        result = execute(
            self.session,
            self.graphql_url,
            QUESTION_DETAILS_QUERY,
            {"titleSlug": slug},
        )

        question = (result.get("data") or {}).get("question")

        if question:
            logger.info(
                "question_details_request_succeeded",
                slug=slug,
                title=question.get("title"),
            )
        else:
            logger.warning("question_details_not_found", slug=slug)

        return result

    def get_submission_list(
        self,
        slug: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return submission history for a problem."""
        self.auth.ensure_authenticated(self.session)

        result = execute(
            self.session,
            self.graphql_url,
            SUBMISSION_LIST_QUERY,
            {
                "questionSlug": slug,
                "limit": limit,
                "offset": 0,
            },
        )

        submissions = (
            (result.get("data") or {})
            .get("questionSubmissionList", {})
            .get("submissions", [])
        )

        logger.info(
            "submission_list_request_succeeded",
            slug=slug,
            submission_count=len(submissions),
        )

        return result

    def get_recent_ac_submissions(
        self,
        username: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return a user's most recent accepted submissions."""
        self.auth.ensure_authenticated(self.session)

        target_username = username or self.settings.USERNAME

        if not target_username:
            raise ValueError(
                "No LeetCode username available — "
                "pass one explicitly or set LEETCODE_USERNAME."
            )

        result = execute(
            self.session,
            self.graphql_url,
            RECENT_AC_SUBMISSIONS_QUERY,
            {
                "username": target_username,
                "limit": limit,
            },
        )

        submissions = (result.get("data") or {}).get("recentAcSubmissionList") or []

        if not submissions:
            logger.warning(
                "recent_ac_submissions_empty_result",
                action="forcing_auth_recheck",
            )
            self.auth.ensure_authenticated(self.session, force=True)

        logger.info(
            "recent_ac_submissions_request_succeeded",
            username=target_username,
            submission_count=len(submissions),
        )

        return result

    def get_submission_details(
        self,
        submission_id: int,
    ) -> dict[str, Any]:
        """Return source code and metadata for a submission."""
        self.auth.ensure_authenticated(self.session)

        result = execute(
            self.session,
            self.graphql_url,
            SUBMISSION_DETAILS_QUERY,
            {"submissionId": int(submission_id)},
        )

        logger.info(
            "submission_details_request_succeeded",
            submission_id=submission_id,
        )

        return result

    @staticmethod
    def _parse_solved_problem(
        pair: dict[str, Any],
    ) -> dict[str, Any]:
        stat = pair.get("stat") or {}
        difficulty = pair.get("difficulty") or {}

        return {
            "slug": stat.get("question__title_slug"),
            "id": stat.get("frontend_question_id"),
            "title": stat.get("question__title"),
            "difficulty": DIFFICULTY_LEVELS.get(
                difficulty.get("level", 0),
                "Unknown",
            ),
        }
