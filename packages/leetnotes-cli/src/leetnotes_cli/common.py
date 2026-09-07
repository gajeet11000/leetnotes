"""Helpers shared across more than one command module."""

import random
import time

import click
import structlog
from leetnotes_core.leetcode.pipeline import LeetCodeSyncManager

logger = structlog.get_logger(__name__)

_manager_instance: LeetCodeSyncManager | None = None


def get_manager() -> LeetCodeSyncManager:
    """Lazily builds the shared sync manager, so `--help` never touches disk/network setup."""
    global _manager_instance
    if _manager_instance is None:
        logger.info("sync_manager_initialized")
        _manager_instance = LeetCodeSyncManager()
    return _manager_instance


# --------------------------------------------------------------------------- #
# Per-part fetch status (used by `problems data fetch` and `notes render` —
# anywhere that runs the description/images/submission pipeline parts and
# needs to report, per part, whether it was already there, freshly fetched,
# or failed, rather than one vague "fetching..." line).
# --------------------------------------------------------------------------- #

PART_METHODS = {
    "description": "populate_question_metadata",
    "images": "populate_question_images",
    "submission": "populate_submission_code",
}
PART_ORDER = ("description", "images", "submission")


def is_part_populated(mgr: LeetCodeSyncManager, part_name: str, slug: str) -> bool:
    """Whether `part_name` already has data for `slug`, without touching the network."""
    log = logger.bind(slug=slug, stage=part_name)

    if part_name == "submission":
        exists = mgr.storage.submissions_exists(slug)
        # A submission can exist but still be cache-pending — e.g. reopened by
        # reconcile_recent_accepted because a fresher accepted submission was seen.
        reopened = mgr.storage.is_part_pending(slug, "submission")
        found = exists and not reopened
        log.info(
            "part_populated_check",
            already_populated=found,
            exists=exists,
            reopened=reopened,
        )
        return found

    record = mgr.storage.problems_get_by_slug(slug)
    if record is None:
        log.info(
            "part_populated_check",
            already_populated=False,
            reason="no_problem_record_stored",
        )
        return False
    if part_name == "description":
        found = bool(record.raw_question_html)
    elif part_name == "images":
        # A question can legitimately have zero images (has_images=False,
        # done) or have images that all failed to download so far
        # (has_images=True, imgs_local_paths still empty, worth retrying) —
        # images_populated tells the two apart instead of just checking
        # imgs_local_paths truthiness.
        found = record.images_populated
    else:
        raise ValueError(f"Unknown part: {part_name}")

    log.info("part_populated_check", already_populated=found)
    return found


def run_part_for_slug(
    mgr: LeetCodeSyncManager, part_name: str, slug: str, refetch: bool
) -> str:
    """Runs one pipeline part for one slug. Returns 'skipped', 'success', or 'failed'."""
    with structlog.contextvars.bound_contextvars(slug=slug, stage=part_name):
        log = logger.bind()

        if not refetch and is_part_populated(mgr, part_name, slug):
            log.info("part_fetch_skipped", reason="already_populated_using_stored_data")
            return "skipped"

        log.info("part_fetch_started", refetch=refetch)
        method = getattr(mgr, PART_METHODS[part_name])
        succeeded = method(slug, force_update=refetch)

        status = "success" if succeeded else "failed"
        log.info("part_fetch_finished", status=status)
        return status


def describe_part_status(part_name: str, slug: str, status: str) -> str:
    labels = {"success": "done", "skipped": "skip", "failed": "fail"}
    return f"[{labels[status]:>4}] {part_name:<10} {slug}"


# --------------------------------------------------------------------------- #
# Pending-slug status tags — derived purely from local state (pending_cache +
# the submissions table), so it's available identically to every command
# that lists or picks pending slugs, live sync or not, online or offline.
# --------------------------------------------------------------------------- #


def pending_status_tag(
    mgr: LeetCodeSyncManager, slug: str, cache_entry: dict
) -> str | None:
    """(new)/(updated)/None for one pending_cache entry:

    - "(new)": nothing's been fetched for this slug at all yet (description
      still outstanding — images/submission can't have run either, since
      both depend on description having run first).
    - "(updated)": specifically a previously-stored submission that's gone
      stale — reopened by LeetCodeSyncManager.reconcile_recent_accepted
      because a fresher accepted submission was seen — rather than a
      first-time fetch. Distinguished from "still just in progress" by
      whether a submissions-table row already exists for this slug.
    - None: something's already been fetched (so not "new"), and submission
      being outstanding isn't a resubmission (so not "updated" either) —
      just an ordinary in-progress slug.
    """
    if not cache_entry.get("description") and not cache_entry.get("images"):
        return "(new)"
    if not cache_entry.get("submission") and mgr.storage.submissions_exists(slug):
        return "(updated)"
    return None


def pending_tags(
    mgr: LeetCodeSyncManager, pending_cache: dict[str, dict]
) -> dict[str, str]:
    """slug -> tag for every pending_cache entry that has one (see pending_status_tag)."""
    return {
        slug: tag
        for slug, entry in pending_cache.items()
        if (tag := pending_status_tag(mgr, slug, entry)) is not None
    }


_TAG_PRIORITY = {"(new)": 0, "(updated)": 1}


def order_candidates(
    slugs: list[str], pending_cache: dict[str, dict], tags: dict[str, str]
) -> list[str]:
    """Orders slugs so whatever needs attention surfaces first: (new)-tagged,
    then (updated)-tagged, then any other still-pending slug, then anything
    already fully fetched — alphabetical within each group. Used by every
    picker/listing that mixes pending and already-fetched slugs, so "what's
    outstanding" is always at the top regardless of which command it is."""

    def group(slug: str) -> int:
        if slug not in pending_cache:
            return 3
        tag = tags.get(slug)
        return _TAG_PRIORITY.get(tag, 2)

    return sorted(slugs, key=lambda s: (group(s), s))


class CircuitBreaker:
    """Trips after too many consecutive failures in a batch loop.

    Guards against grinding through hundreds of remaining slugs once we're
    actually being rate-limited or blocked rather than hitting isolated,
    one-off errors — a stray failure resets the counter, but a sustained run
    of them trips it so the caller can stop early instead of burning through
    the rest of the batch on a lost cause.
    """

    def __init__(self, max_consecutive_failures: int):
        self.max_consecutive_failures = max_consecutive_failures
        self._consecutive_failures = 0

    def record(self, failed: bool) -> None:
        self._consecutive_failures = self._consecutive_failures + 1 if failed else 0

    @property
    def tripped(self) -> bool:
        if self.max_consecutive_failures <= 0:
            return False  # disabled
        return self._consecutive_failures >= self.max_consecutive_failures


class BatchPacer:
    """Inserts a randomized cooldown after every `batch_size` items in a big
    --all run.

    Without this, a large backlog (e.g. hundreds of pending problems) runs as
    one long, uninterrupted burst of requests — itself a suspicious traffic
    shape regardless of error rate. Breaking it into several short,
    human-shaped sessions automatically means the user doesn't have to
    babysit and re-invoke the command themselves to get the same effect.
    """

    def __init__(
        self, batch_size: int, cooldown_range: tuple[float, float] = (60, 120)
    ):
        self.batch_size = batch_size
        self.cooldown_range = cooldown_range

    def should_pause_after(self, position: int, total: int) -> bool:
        """`position` is 1-based — the count of items processed so far."""
        if self.batch_size <= 0 or position >= total:
            return False
        return position % self.batch_size == 0

    def pause(self, stage: str, position: int, total: int) -> None:
        """Sleeps a random duration within `cooldown_range`, logging + echoing progress."""
        duration = random.uniform(*self.cooldown_range)
        batch_num = position // self.batch_size
        total_batches = -(-total // self.batch_size)  # ceil division

        logger.info(
            "populate_batch_cooldown_started",
            stage=stage,
            batch=batch_num,
            total_batches=total_batches,
            seconds=round(duration, 1),
        )
        click.echo(
            f"\n[pause] batch {batch_num}/{total_batches} done for '{stage}' — "
            f"cooling down {duration:.0f}s before continuing..."
        )
        time.sleep(duration)


def print_batch_summary(
    succeeded: list[str], failed: list[str], skipped: list[str] | None = None
) -> None:
    """Prints a one-line succeeded/skipped/failed summary for a batch command run."""
    parts = [f"{len(succeeded)} succeeded"]
    if skipped is not None:
        parts.append(f"{len(skipped)} skipped")
    parts.append(f"{len(failed)} failed")
    summary = ", ".join(parts)
    if failed:
        summary += f": {failed}"
    click.echo(f"\n{summary}")
