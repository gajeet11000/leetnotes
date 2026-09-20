"""`notes` command group: the "give me notes" entrypoint.

`notes render` is the everyday command — it doesn't care whether problem data
has been fetched yet: it fetches whatever's missing (a no-op for anything
already stored — see LeetCodeSyncManager's populate_* methods), renders the
problem/solution file, optionally generates AI prefill content, and renders
the personal study-notes file, for one slug or a whole batch. It replaces the
old standalone `solve` command — that pipeline now lives here as
`_generate_notes_for_slug`.

Any batch/picker scope (i.e. SLUG omitted) syncs from LeetCode first — new
slugs solved since the last sync, plus reopening `submission` for anything
resubmitted (see LeetCodeSyncManager.sync_pending_cache) — so a slug that's
solved but never locally fetched (including one whose local data was
deleted) still shows up as a candidate. `--recent`/`--today` additionally
scope that (already-synced) batch to LeetCode's recent-accepted-submissions
feed instead of the local slug set. A resubmitted slug's problem file is
always re-rendered as part of this same pipeline, since populate_submission_code
refetches automatically once the pending cache reopens that part — no
separate "stale" handling needed here.

`notes prefill` remains a standalone way to generate AI prefill content
without rendering anything yet.
"""

import time
from pathlib import Path

import click
import requests
import structlog
from leetnotes_core.ai_prefill import (
    AIPrefillGenerator,
    AIProviderError,
    PrefillGenerationError,
)
from leetnotes_core.ai_prefill.settings import ai_prefill_settings
from leetnotes_core.leetcode.pipeline import LeetCodeSyncManager
from leetnotes_core.render.markdown_notes import LeetCodeDSAProblemNotesRender
from leetnotes_core.render.markdown_problem import (
    ImagesNotReadyError,
    LeetCodeDSAProblemMarkdownRender,
)
from leetnotes_core.render.settings import render_settings
from leetnotes_core.render.utils import AI_STYLE, NotesStyle

from .common import (
    CircuitBreaker,
    describe_part_status,
    get_manager,
    order_candidates,
    pending_tags,
    print_batch_summary,
    run_part_for_slug,
)
from .picker import label_records, label_slugs, pick_slugs
from .root import cli

logger = structlog.get_logger(__name__)


@cli.group()
def notes() -> None:
    """Render personal study-notes files, and generate AI prefill content for them."""


# --------------------------------------------------------------------------- #
# `notes render` — scope resolution
# --------------------------------------------------------------------------- #


def _known_slug_candidates(mgr: LeetCodeSyncManager) -> list[tuple[str, str]]:
    """(slug, label) pairs for every slug worth offering: pending ones first
    — freshly-discovered ("new") ahead of resubmitted ("updated") ahead of
    everything else still in progress — then everything already fully
    populated, at the bottom (see order_candidates). Re-running on an
    already-done slug is a safe refresh, not an error, so it stays offered
    too.

    Labeled consistently via label_slugs — a not-yet-fetched slug gets its
    id/title/difficulty from the pending cache itself (PendingCacheStore
    persists it there on every live sync — see
    LeetCodeSyncManager.sync_pending_cache), and its (new)/(updated) tag from
    pending_tags — both derived from local state alone, so this stays fully
    labeled and ordered even when _sync_and_report degraded to a local-only
    view (e.g. offline).
    """
    pending_cache = mgr.storage.read_pending_cache()
    done = {r.slug for r in mgr.storage.list_all() if r.slug}
    known = {r.slug: r for r in mgr.storage.list_all() if r.slug}
    tags = pending_tags(mgr, pending_cache)
    ordered_slugs = order_candidates(
        sorted(set(pending_cache) | done), pending_cache, tags
    )
    return label_slugs(ordered_slugs, known, solved_meta=pending_cache, tags=tags)


def _offline_sync_result(mgr: LeetCodeSyncManager) -> dict:
    """Same shape as sync_pending_cache()'s return, built from local data only
    — no new/stale detection, since that requires actually comparing against
    LeetCode. Candidate labeling still works fully offline regardless (see
    _known_slug_candidates), since id/title/difficulty live in the persisted
    pending cache, not in this transient result."""
    return {
        "pending_slugs": list(mgr.storage.read_pending_cache().keys()),
        "new_slugs": [],
        "stale_submission_slugs": [],
    }


def _sync_and_report(mgr: LeetCodeSyncManager) -> dict:
    """Runs sync_pending_cache(), reporting what it found. Falls back to a
    local-only view (see _offline_sync_result) if LeetCode can't be reached
    — e.g. offline — rather than blocking the picker entirely on a network
    call it can't make; anything already fetched locally is still pickable."""
    click.echo("Syncing with LeetCode...")
    try:
        result = mgr.sync_pending_cache()
    except requests.exceptions.RequestException as exc:
        logger.warning("sync_and_report_offline_fallback", error=str(exc))
        click.echo(
            f"  could not reach LeetCode ({exc.__class__.__name__}) — using local data only."
        )
        return _offline_sync_result(mgr)
    click.echo(
        f"  {len(result['new_slugs'])} new slug(s) discovered, "
        f"{len(result['stale_submission_slugs'])} resubmission(s) detected."
    )
    return result


def _recent_scope_candidates(
    mgr: LeetCodeSyncManager, today_only: bool
) -> list[tuple[str, str]]:
    """(slug, label) pairs scoped to LeetCode's recent-accepted-submissions
    feed (~20 most recent, or just today's), in the same label format as
    every other picker — kept in the feed's own most-recent-first order
    rather than reordered by order_candidates, since that ordering is itself
    meaningful here. Assumes the caller already synced (see
    _sync_and_report) — this itself still makes a live GraphQL call
    (list_recent_accepted), so unlike _known_slug_candidates this scope has
    no offline fallback."""
    items = mgr.list_recent_accepted(today_only=today_only)
    slugs = [item["slug"] for item in items]
    known = {r.slug: r for r in mgr.storage.list_all() if r.slug}
    pending_cache = mgr.storage.read_pending_cache()
    return label_slugs(
        slugs, known, solved_meta=pending_cache, tags=pending_tags(mgr, pending_cache)
    )


# --------------------------------------------------------------------------- #
# `notes render` — the merged fetch -> render problem -> prefill -> render
# notes pipeline for one slug (formerly the `solve` command's `_solve_one`).
# --------------------------------------------------------------------------- #


def _generate_notes_for_slug(
    mgr: LeetCodeSyncManager,
    slug: str,
    *,
    style: NotesStyle,
    output_base: Path | None,
    replace_existing: bool,
    ai: bool,
    regenerate_ai: bool,
    rate_limit_delay: float,
) -> str:
    """Runs the full pipeline for one slug. Returns the final notes save
    status ('written'/'skipped') — 'skipped' also covers the problem's images
    having failed to download, in which case prefill/notes are skipped too
    (see ImagesNotReadyError). Raises if problem metadata genuinely couldn't
    be fetched — nothing downstream can proceed without it."""
    log = logger.bind(slug=slug)

    description_status = run_part_for_slug(mgr, "description", slug, refetch=False)
    click.echo(f"  {describe_part_status('description', slug, description_status)}")
    record = mgr.storage.problems_get_by_slug(slug)
    if record is None or not record.raw_question_html:
        raise RuntimeError("could not fetch problem metadata")

    for part_name in ("images", "submission"):
        status = run_part_for_slug(mgr, part_name, slug, refetch=False)
        click.echo(f"  {describe_part_status(part_name, slug, status)}")

    combined = mgr.storage.get_combined_by_slug(slug)

    click.echo("  -> render problem file")
    try:
        LeetCodeDSAProblemMarkdownRender(output_base=output_base).save(combined)
    except ImagesNotReadyError as exc:
        click.echo(f"  [skip] found error downloading '{slug}'s images — {exc}")
        return "skipped"

    target_style = style
    if ai or regenerate_ai:
        generator = AIPrefillGenerator()
        if regenerate_ai or not generator.storage.exists(slug):
            click.echo("  -> generating AI prefill content...")
            try:
                generator.generate(combined)
            except (AIProviderError, PrefillGenerationError) as exc:
                log.warning("notes_render_prefill_failed", error=str(exc))
                click.echo(f"     (failed: {exc})")
            else:
                if rate_limit_delay:
                    time.sleep(rate_limit_delay)
        if generator.storage.exists(slug):
            target_style = AI_STYLE[style]
        else:
            log.warning(
                "notes_render_ai_style_skipped", reason="no_prefill_content_available"
            )
            click.echo("     (no prefill content available — writing without it)")

    click.echo(f"  -> render notes file ({target_style.value})")
    notes_renderer = LeetCodeDSAProblemNotesRender(
        style=target_style, output_base=output_base
    )
    _, status = notes_renderer.save(combined, replace_existing=replace_existing)
    return status


@notes.command("render")
@click.argument("slug", required=False)
@click.option(
    "--all",
    "run_all",
    is_flag=True,
    help="Skip the interactive picker and process every slug in scope — the "
    "--recent/--today batch if given, otherwise every known slug (pending or "
    "already-populated). Can be a long-running batch, especially combined "
    "with --ai — consider picking interactively instead for anything but a "
    "small backlog.",
)
@click.option(
    "--recent",
    "recent_scope",
    is_flag=True,
    help="Scope to LeetCode's recent-accepted-submissions batch (~20 most "
    "recent), syncing from LeetCode first to catch brand-new slugs and "
    "resubmits.",
)
@click.option(
    "--today",
    "today_scope",
    is_flag=True,
    help="Scope to submissions accepted today (local time), syncing from "
    "LeetCode first to catch brand-new slugs and resubmits.",
)
@click.option(
    "--style",
    type=click.Choice([NotesStyle.PLAIN.value, NotesStyle.OBSIDIAN.value]),
    default=render_settings.DEFAULT_NOTES_STYLE,
    show_default=True,
    help="Base notes style. With --ai/--regenerate-ai, the '+ai' variant of "
    "this is used instead. Defaults to DEFAULT_NOTES_STYLE (.env) when set.",
)
@click.option(
    "--ai",
    is_flag=True,
    help="Render the '+ai' variant of --style, using the latest stored AI "
    "prefill content for each slug — generating it first if none exists yet.",
)
@click.option(
    "--regenerate-ai",
    "regenerate_ai",
    is_flag=True,
    help="Like --ai, but always generates a fresh prefill version first, even "
    "if one already exists (prior versions are kept, not overwritten — see "
    "'notes prefill').",
)
@click.option(
    "--replace-existing",
    is_flag=True,
    help="Regenerate the notes file even if one already exists — the existing "
    "file is backed up first (Leetcode Notes/backups/<id>-<slug>/), since it "
    "may contain hand-written content.",
)
@click.option(
    "--output-base",
    "output_base",
    type=click.Path(path_type=Path),
    default=None,
    help="Priority: this flag > OUTPUT_BASE_DIR (.env) > render_settings.DEFAULT_WRITE_DIR.",
)
@click.option(
    "--no-rate-limit",
    "no_rate_limit",
    is_flag=True,
    help="With --ai/--regenerate-ai, skip the AI_PREFILL_RATE_LIMIT_SECONDS "
    "pause between AI generations.",
)
@click.option(
    "--max-failures",
    "max_failures",
    type=int,
    default=5,
    show_default=True,
    help="With --all, abort the run after this many consecutive failures. 0 disables.",
)
def notes_render(
    slug: str | None,
    run_all: bool,
    recent_scope: bool,
    today_scope: bool,
    style: str,
    ai: bool,
    regenerate_ai: bool,
    replace_existing: bool,
    output_base: Path | None,
    no_rate_limit: bool,
    max_failures: int,
) -> None:
    """Fetch (if needed), render the problem file, and render the study-notes
    file for one or more slugs — the everyday "give me notes" command.

    Omit SLUG, --all, --recent, and --today to pick interactively instead — a
    searchable, multi-select prompt over every known slug.
    """
    if slug and (run_all or recent_scope or today_scope):
        raise click.UsageError(
            "Pass SLUG on its own, not combined with --all/--recent/--today."
        )
    if recent_scope and today_scope:
        raise click.UsageError("Pass either --recent or --today, not both.")

    mgr = get_manager()
    rate_limit_delay = 0.0 if no_rate_limit else ai_prefill_settings.RATE_LIMIT_SECONDS
    kwargs = {
        "style": NotesStyle(style),
        "output_base": output_base,
        "replace_existing": replace_existing,
        "ai": ai,
        "regenerate_ai": regenerate_ai,
        "rate_limit_delay": rate_limit_delay,
    }

    if slug:
        with structlog.contextvars.bound_contextvars(slug=slug, stage="notes"):
            log = logger.bind()
            log.info("notes_render_command_started")
            try:
                status = _generate_notes_for_slug(mgr, slug, **kwargs)
            except Exception as exc:  # noqa: BLE001 — the pipeline spans
                # network, disk, and template rendering; any failure here
                # should surface as a clean CLI error, not a raw traceback.
                log.warning("notes_render_command_failed", error=str(exc))
                raise click.ClickException(
                    f"could not render notes for '{slug}': {exc}"
                )
            log.info("notes_render_command_succeeded", status=status)
            click.echo(f"[{'done' if status == 'written' else 'skip'}] notes {slug}")
        return

    # Always sync first when resolving a batch/picker scope (SLUG already
    # returned above — a single explicit slug fetches directly and doesn't
    # need the candidate list). Without this, a slug solved on LeetCode but
    # never locally fetched (including one whose local data was deleted)
    # wouldn't appear in the picker at all.
    _sync_and_report(mgr)
    if recent_scope or today_scope:
        # Unlike the default scope, --recent/--today has no local fallback —
        # LeetCode's recent-accepted feed isn't cached anywhere, so this
        # genuinely can't work offline.
        try:
            candidates = _recent_scope_candidates(mgr, today_only=today_scope)
        except requests.exceptions.RequestException as exc:
            raise click.ClickException(
                f"could not reach LeetCode to fetch the recent-accepted feed: {exc}"
            )
    else:
        candidates = _known_slug_candidates(mgr)

    if not candidates:
        click.echo("Nothing to do — no slugs in scope.")
        return

    if run_all:
        slugs = [candidate_slug for candidate_slug, _ in candidates]
    else:
        slugs = pick_slugs(candidates)
        if not slugs:
            click.echo("Nothing selected.")
            return

    logger.info("notes_render_batch_started", slug_count=len(slugs))
    succeeded, failed = [], []
    breaker = CircuitBreaker(max_failures)
    total = len(slugs)
    for idx, target_slug in enumerate(slugs):
        click.echo(f"[{idx + 1}/{total}] {target_slug}")
        with structlog.contextvars.bound_contextvars(slug=target_slug, stage="notes"):
            try:
                status = _generate_notes_for_slug(mgr, target_slug, **kwargs)
            except Exception as exc:
                logger.exception("notes_render_command_failed")
                click.echo(f"  [fail] {exc}")
                failed.append(target_slug)
                breaker.record(True)
            else:
                click.echo(f"  [{'done' if status == 'written' else 'skip'}]")
                succeeded.append(target_slug)
                breaker.record(False)

        if breaker.tripped:
            remaining = total - idx - 1
            logger.warning(
                "notes_render_batch_aborted",
                reason="too_many_consecutive_failures",
                max_consecutive_failures=max_failures,
                remaining_slug_count=remaining,
            )
            click.echo(
                f"\n[abort] {max_failures} consecutive failures — stopping early, "
                f"{remaining} slug(s) left unprocessed."
            )
            break

    logger.info(
        "notes_render_batch_completed",
        succeeded_count=len(succeeded),
        failed_count=len(failed),
    )
    print_batch_summary(succeeded, failed)


# --------------------------------------------------------------------------- #
# `notes prefill`
# --------------------------------------------------------------------------- #


@notes.command("prefill")
@click.argument("slug", required=False)
@click.option(
    "--all",
    "run_all",
    is_flag=True,
    help="Generate prefill content for every slug that already has problem data populated.",
)
@click.option(
    "--regenerate",
    is_flag=True,
    help="Generate a new version even if prefill content already exists for this "
    "slug. Prior versions are kept either way — see AIPrefillStorage.",
)
@click.option(
    "--no-rate-limit",
    "no_rate_limit",
    is_flag=True,
    help="With --all, skip the AI_PREFILL_RATE_LIMIT_SECONDS pause between "
    "generations. Only worth it on a plan without tight usage limits.",
)
@click.option(
    "--limit",
    "limit",
    type=int,
    default=None,
    help="With --all, cap the run to at most this many slugs.",
)
@click.option(
    "--max-failures",
    "max_failures",
    type=int,
    default=5,
    show_default=True,
    help="With --all, abort the run after this many consecutive failures. 0 disables.",
)
def notes_prefill(
    slug: str | None,
    run_all: bool,
    regenerate: bool,
    no_rate_limit: bool,
    limit: int | None,
    max_failures: int,
) -> None:
    """Generate AI prefill content (pattern/core idea/invariant/...) for stored problem(s).

    Uses whichever CLI AI tool is configured via AI_PREFILL_PROVIDER (Claude
    Code's `claude -p` by default — see modules/ai_prefill/providers). Every
    generation is appended to that slug's version history rather than
    overwriting a previous attempt.

    Omit both SLUG and --all to pick interactively instead — a searchable,
    multi-select prompt over every slug with problem data populated.
    """
    if slug and run_all:
        raise click.UsageError("Pass either SLUG or --all, not both.")

    mgr = get_manager()
    generator = AIPrefillGenerator()
    delay = 0.0 if no_rate_limit else ai_prefill_settings.RATE_LIMIT_SECONDS

    if slug:
        with structlog.contextvars.bound_contextvars(slug=slug, stage="prefill"):
            log = logger.bind()
            record = mgr.storage.get_combined_by_slug(slug)
            if record is None or not record.raw_question_html:
                log.warning("prefill_command_skipped", reason="no_problem_data_stored")
                raise click.ClickException(
                    f"no problem data found for '{slug}', run 'problems data fetch {slug}' first"
                )
            if not regenerate and generator.storage.exists(slug):
                log.info("prefill_command_skipped", reason="prefill_already_exists")
                click.echo(
                    f"[skip] prefill {slug} (already has prefill content, "
                    f"use --regenerate to add another version)"
                )
                return
            try:
                generator.generate(record)
            except (AIProviderError, PrefillGenerationError) as exc:
                log.warning("prefill_command_failed", error=str(exc))
                raise click.ClickException(
                    f"prefill generation failed for '{slug}': {exc}"
                )
            log.info("prefill_command_succeeded")
            click.echo(f"[done] prefill {slug}")
        return

    records = [r for r in mgr.storage.list_all_combined() if r.raw_question_html]

    if run_all:
        if limit:
            records = records[:limit]
    else:
        if not records:
            click.echo(
                "Nothing to pick from — no slugs have problem data populated yet."
            )
            return
        picked = pick_slugs(label_records(records))
        if not picked:
            click.echo("Nothing selected.")
            return
        records = [r for r in records if r.slug in picked]

    if not records:
        logger.info("prefill_command_batch_completed", reason="no_problem_data_stored")
        click.echo("Nothing to do — no slugs have problem data populated yet.")
        return

    logger.info("prefill_command_batch_started", record_count=len(records))
    succeeded, skipped, failed = [], [], []
    breaker = CircuitBreaker(max_failures)
    total = len(records)
    for idx, record in enumerate(records):
        with structlog.contextvars.bound_contextvars(slug=record.slug, stage="prefill"):
            if not regenerate and generator.storage.exists(record.slug):
                click.echo(
                    f"[skip] {record.slug} (already has prefill content, "
                    f"use --regenerate to add another version)"
                )
                skipped.append(record.slug)
                breaker.record(False)
            else:
                try:
                    generator.generate(record)
                except (AIProviderError, PrefillGenerationError) as exc:
                    logger.exception("prefill_command_failed")
                    click.echo(f"[fail] {record.slug}: {exc}")
                    failed.append(record.slug)
                    breaker.record(True)
                else:
                    click.echo(f"[done] {record.slug}")
                    succeeded.append(record.slug)
                    breaker.record(False)
                    if delay and idx < total - 1:
                        time.sleep(delay)

        if breaker.tripped:
            remaining = total - idx - 1
            logger.warning(
                "prefill_batch_aborted",
                reason="too_many_consecutive_failures",
                max_consecutive_failures=max_failures,
                remaining_slug_count=remaining,
            )
            click.echo(
                f"\n[abort] {max_failures} consecutive failures — stopping early, "
                f"{remaining} slug(s) left unprocessed."
            )
            break

    logger.info(
        "prefill_command_batch_completed",
        succeeded_count=len(succeeded),
        skipped_count=len(skipped),
        failed_count=len(failed),
    )
    print_batch_summary(succeeded, failed, skipped)
