import structlog

from leetnotes_core.leetcode import parsers
from leetnotes_core.leetcode.client import LeetCodeClient
from leetnotes_core.leetcode.image_processor import LeetCodeImageProcessor
from leetnotes_core.leetcode.models import ProblemRecord, SubmissionRecord
from leetnotes_core.leetcode.recent_activity import dedupe_latest_per_slug, filter_today
from leetnotes_core.leetcode.storage import LeetCodeDSAStorage

logger = structlog.get_logger(__name__)


class LeetCodeSyncManager:
    def __init__(
        self,
        client: LeetCodeClient | None = None,
        storage: LeetCodeDSAStorage | None = None,
        image_processor: LeetCodeImageProcessor | None = None,
    ):
        self.client = client or LeetCodeClient()
        self.storage = storage or LeetCodeDSAStorage()
        self.image_processor = image_processor or LeetCodeImageProcessor()

    # ------------------------------------------------------------------ #
    # Step 1: discover solved problems (cache-backed)
    # ------------------------------------------------------------------ #

    def sync_pending_cache(self) -> dict:
        """
        The full "pending sync" operation, always live (no cache-only mode —
        for a free/local view of the cache, read it directly instead):

          1. Reconciles the pending cache against actual stored data.
          2. Hits LeetCode's complete solved-questions list and merges any
             newly-solved slugs in — the backstop that catches everything
             ever solved, regardless of how long it's been since the last
             sync.
          3. Reconciles against the recent-accepted-submissions feed (see
             reconcile_recent_accepted) to catch resubmits of already-stored
             problems, which the complete solved-list alone can't detect
             (it has no timestamps).

        Note: this only populates the pending cache, never the DB. A DB record
        for a slug is created the first time populate_question_metadata actually
        fetches real data for it — the DB should only ever hold slugs with at
        least one populated part.

        Returns {"pending_slugs", "new_slugs", "stale_submission_slugs"}.

        The complete-solved-list endpoint also carries id/title/difficulty
        for every solved slug at no extra request cost — that's persisted
        straight into the pending cache (PendingCacheStore.refresh_pending_cache's
        `meta`) rather than returned here, so a picker built on
        storage.read_pending_cache() can label a not-yet-fetched slug
        consistently even without a live sync (e.g. offline).
        """
        log = logger.bind(stage="pending_sync")
        self.storage.reconcile_pending_cache()
        pending_before = set(self.storage.read_pending_cache().keys())

        log.info("solved_slugs_refresh_started")
        solved_problems = self.client.get_solved_questions()
        solved_problem_slugs = [p["slug"] for p in solved_problems if p["slug"]]
        solved_meta = {p["slug"]: p for p in solved_problems if p["slug"]}
        self.storage.refresh_pending_cache(solved_problem_slugs, meta=solved_meta)
        log.info(
            "solved_slugs_refresh_completed", fetched_count=len(solved_problem_slugs)
        )

        stale_submission_slugs = self.reconcile_recent_accepted()

        pending_slugs = list(self.storage.read_pending_cache().keys())
        new_slugs = sorted(set(pending_slugs) - pending_before)

        log.info(
            "pending_sync_completed",
            pending_count=len(pending_slugs),
            new_count=len(new_slugs),
            stale_submission_count=len(stale_submission_slugs),
        )
        return {
            "pending_slugs": pending_slugs,
            "new_slugs": new_slugs,
            "stale_submission_slugs": stale_submission_slugs,
        }

    # ------------------------------------------------------------------ #
    # Part 1: Question metadata + content (description)
    # ------------------------------------------------------------------ #

    def populate_question_metadata(self, slug: str, force_update: bool = False) -> bool:
        """
        Fetches question metadata + description content (via GraphQL) and stores it.
        Marks 'description' as fetched in the pending cache on success.

        If metadata already exists and force_update is False, this is a no-op.
        """
        with structlog.contextvars.bound_contextvars(slug=slug, stage="description"):
            existing_record = self.storage.problems_get_by_slug(slug)

            has_metadata = existing_record is not None and bool(
                existing_record.raw_question_html
            )
            if has_metadata and not force_update:
                logger.info(
                    "description_already_populated",
                    question_id=existing_record.id,
                    title=existing_record.title,
                )
                return False

            logger.info("description_fetch_started", force_update=force_update)
            gql_data = self.client.get_question_details(slug)
            if not gql_data:
                logger.warning("description_fetch_failed", reason="no_data_returned")
                return False

            parsed_data = parsers.gql_question_data(gql_data)

            # Preserve fields owned by the other part of this store (images).
            preserved = {}
            if existing_record:
                preserved["imgs_local_paths"] = existing_record.imgs_local_paths

            question_record = ProblemRecord(**parsed_data, **preserved)
            question_record.content.text = parsers.html_to_plain_text(
                question_record.raw_question_html
            )
            question_record.content.remote_markdown = parsers.html_to_markdown(
                question_record.raw_question_html
            )
            question_record.content.local_markdown = (
                question_record.content.remote_markdown
            )
            question_record.content.local_html = question_record.raw_question_html

            self.storage.problems_add_or_update(question_record)
            self.storage.mark_part_fetched(slug, "description")

            logger.info(
                "description_fetch_succeeded",
                question_id=question_record.id,
                title=question_record.title,
            )
            return True

    # ------------------------------------------------------------------ #
    # Part 2: Question images
    # ------------------------------------------------------------------ #

    def populate_question_images(self, slug: str, force_update: bool = False) -> bool:
        """
        Downloads and caches question images (if any) and derives the
        image-localized HTML/Markdown content. Requires metadata (raw_question_html)
        to already exist — run populate_question_metadata first.
        Marks 'images' as fetched in the pending cache on completion, including
        when the question turns out to have no images (that's still a resolved state).

        If images already exist and force_update is False, this is a no-op.
        """
        with structlog.contextvars.bound_contextvars(slug=slug, stage="images"):
            existing_record = self.storage.problems_get_by_slug(slug)

            if not existing_record or not existing_record.raw_question_html:
                logger.warning(
                    "images_fetch_skipped", reason="problem_metadata_missing"
                )
                return False

            if existing_record.images_populated and not force_update:
                logger.info(
                    "images_already_populated",
                    has_images=existing_record.has_images,
                    image_count=len(existing_record.imgs_local_paths or []),
                )
                return False

            logger.info("images_processing_started", force_update=force_update)
            image_result = self.image_processor.process_question_images(
                question_record=existing_record
            )

            existing_record.has_images = image_result["has_images"]
            existing_record.imgs_local_paths = image_result["imgs_local_paths"]
            if image_result["content_local_html"] is not None:
                existing_record.content.local_html = image_result["content_local_html"]
                existing_record.content.local_markdown = parsers.html_to_markdown(
                    image_result["content_local_html"]
                )

            self.storage.problems_add_or_update(existing_record)
            self.storage.mark_part_fetched(slug, "images")

            logger.info(
                "images_fetch_succeeded",
                has_images=existing_record.has_images,
                image_count=len(existing_record.imgs_local_paths or []),
            )
            return True

    # ------------------------------------------------------------------ #
    # Part 3: Submission code details
    # ------------------------------------------------------------------ #

    def _get_accepted_submission_id(self, submission_list: list) -> int | None:
        for submission_data in submission_list:
            if submission_data.get("statusDisplay") == "Accepted":
                return submission_data.get("id")
        return None

    def populate_submission_code(self, slug: str, force_update: bool = False) -> bool:
        """
        Fetches the latest accepted submission (language, code, date) and stores it.
        Marks 'submission' as fetched in the pending cache on success.
        Deliberately does NOT mark the part complete if no accepted submission is
        found yet — the user may submit an accepted solution later.

        If submission data already exists and force_update is False, this is a
        no-op — unless the pending cache has the 'submission' part marked as
        outstanding (see reconcile_recent_accepted), in which case it's
        refetched regardless.

        If the submission is pinned, skip fetching entirely.
        """
        with structlog.contextvars.bound_contextvars(slug=slug, stage="submission"):
            existing_submission = self.storage.submissions_get_by_slug(slug)

            # Check if submission is pinned - if so, skip fetching entirely
            if existing_submission is not None:
                pin_status = self.storage.submissions_get_pin_status(slug)
                if pin_status:
                    logger.info(
                        "submission_pin_skipped",
                        lang=existing_submission.lang if existing_submission else "unknown",
                        submission_date=str(existing_submission.submission_date) if existing_submission else "unknown",
                    )
                    return False

            # The pending cache can say "submission" is outstanding even though
            # a submission record already exists — e.g. reconcile_recent_accepted
            # reopened it because a fresher accepted submission was seen. That
            # should be refetched even without an explicit force_update.
            still_pending_in_cache = self.storage.is_part_pending(slug, "submission")
            has_submission = (
                existing_submission is not None and not still_pending_in_cache
            )
            if has_submission and not force_update:
                logger.info(
                    "submission_already_populated",
                    lang=existing_submission.lang,
                    submission_date=str(existing_submission.submission_date),
                )
                return False

            logger.info("submission_fetch_started", force_update=force_update)
            submission_list_result = self.client.get_submission_list(slug)
            submission_list = parsers.gql_submission_list(submission_list_result)
            accepted_submission_id = self._get_accepted_submission_id(submission_list)

            if not accepted_submission_id:
                if self.storage.is_known_solved(slug):
                    # LeetCode's own solved-list/recent-accepted feed already
                    # told us this slug has an accepted submission — coming
                    # back empty now contradicts that, so it's the session
                    # going stale, not a real "not solved yet" case (see
                    # is_known_solved). Force a live, uncached re-check
                    # rather than silently believing this empty result.
                    logger.warning(
                        "submission_fetch_suspicious_empty_result",
                        reason="slug_known_solved_but_no_accepted_submission_returned",
                    )
                    self.client.ensure_authenticated(force=True)
                    # Still authenticated: a genuine (rare) inconsistency —
                    # fall through to the normal "not found yet" handling
                    # below, which just leaves the part pending for retry.
                logger.warning(
                    "submission_fetch_failed", reason="no_accepted_submission_found"
                )
                return False

            submission_details_result = self.client.get_submission_details(
                accepted_submission_id
            )
            submission_data = parsers.gql_submission_data(submission_details_result)
            submission_record = SubmissionRecord(slug=slug, **submission_data)

            self.storage.submissions_add_or_update(submission_record)
            # Only mark as fetched if not pinned (though we already checked above)
            if not self.storage.submissions_get_pin_status(slug):
                self.storage.mark_part_fetched(slug, "submission")

            logger.info("submission_fetch_succeeded", lang=submission_record.lang)
            return True

    # ------------------------------------------------------------------ #
    # Recent accepted submissions (LeetCode's recentAcSubmissionList feed).
    #
    # Separate from the solved-slugs pending cache flow above: it doesn't
    # discover the full solved list, it surfaces what was *just* accepted,
    # with a timestamp — good for a "what did I solve today" report, and
    # for noticing an already-synced problem has a fresher accepted
    # submission than what's stored.
    # ------------------------------------------------------------------ #

    def _fetch_recent_accepted(self, limit: int, today_only: bool) -> list[dict]:
        """
        Fetches, parses, optionally filters to today (local time), and
        dedupes (one entry per slug, latest timestamp) the recent accepted-
        submissions feed. Shared by list_recent_accepted and
        reconcile_recent_accepted — this part never touches storage.

        Note: LeetCode's recentAcSubmissionList query appears to cap out
        around 20 results regardless of `limit` — don't rely on a higher
        limit to widen the reconciliation window.
        """
        log = logger.bind(stage="recent")
        log.info("recent_accepted_fetch_started", limit=limit, today_only=today_only)

        raw = self.client.get_recent_ac_submissions(limit=limit)
        submissions = parsers.gql_recent_ac_submissions(raw)

        if today_only:
            submissions = filter_today(submissions)

        submissions = dedupe_latest_per_slug(submissions)
        log.info("recent_accepted_fetch_completed", submission_count=len(submissions))
        return submissions

    def list_recent_accepted(
        self, limit: int = 20, today_only: bool = False
    ) -> list[dict]:
        """
        Returns recently-accepted submissions as {slug, title, timestamp}
        dicts — the full recent-accepted batch by default, optionally
        narrowed to today (local time). Read-only — never touches stored
        state, safe to call as often as you like.
        """
        return self._fetch_recent_accepted(limit, today_only)

    def reconcile_recent_accepted(self, limit: int = 20) -> list[str]:
        """
        Fetches the recent-accepted-submissions feed — always the full
        batch LeetCode returns, never filtered to today, since it's the
        only source with per-submission timestamps and there's no reason to
        throw away comparison data that cost the same one API call to get —
        and reopens the 'submission' part for any slug whose stored
        submission is older than what's now accepted (i.e. it was resolved
        again since the last fetch).

        Brand-new slugs are deliberately not handled here — the complete
        solved-list refresh (see sync_pending_cache) already catches those;
        this only ever reopens 'submission' on slugs already known.

        Returns the list of slugs whose 'submission' part was reopened.
        """
        log = logger.bind(stage="pending_sync")
        submissions = self._fetch_recent_accepted(limit, today_only=False)

        stale_submission_slugs = []
        for item in submissions:
            slug = item["slug"]
            existing_submission = self.storage.submissions_get_by_slug(slug)
            if (
                existing_submission is not None
                and existing_submission.submission_date < item["timestamp"]
            ):
                stale_submission_slugs.append(slug)

        for slug in stale_submission_slugs:
            # Only reopen submission part if not pinned
            if not self.storage.submissions_get_pin_status(slug):
                self.storage.reopen_part(slug, "submission")

        log.info(
            "recent_accepted_reconciled",
            checked_count=len(submissions),
            stale_submission_count=len(stale_submission_slugs),
        )
        return stale_submission_slugs
