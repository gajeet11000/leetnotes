"""`problems pin` command group: pin or unpin problem submissions."""

import click
import structlog

from .common import (
    get_manager,
    print_batch_summary,
)
from .picker import (
    label_records,
    pick_slugs,
)
from .problems import problems

logger = structlog.get_logger(__name__)


def _pick_submission_slugs(mgr) -> list[str]:
    """Interactive multi-select fallback over every stored slug that has a submission."""
    records = [r for r in mgr.storage.list_all_combined() if r.submission is not None]
    if not records:
        click.echo("Nothing to pick from — no stored submissions.")
        return []
    picked = pick_slugs(label_records(records))
    if not picked:
        click.echo("Nothing selected.")
    return picked


def _pin_one(mgr, slug: str) -> bool:
    """Pin a submission for a slug. Returns True if pinned."""
    with structlog.contextvars.bound_contextvars(slug=slug, stage="problems"):
        logger.info("problems_pin_command_started")
        pinned = mgr.storage.submissions_set_pin_status(slug, True)
        if pinned:
            # If pinning and there's a pending submission part, remove it from cache
            if mgr.storage.is_part_pending(slug, "submission"):
                mgr.storage.remove_from_cache(slug)
                logger.info("pending_submission_part_removed_due_to_pin", slug=slug)
            logger.info("submission_pinned", slug=slug)
        else:
            logger.info("problems_pin_command_skipped", reason="not_found")
        return pinned


def _unpin_one(mgr, slug: str) -> bool:
    """Unpin a submission for a slug. Returns True if unpinned."""
    with structlog.contextvars.bound_contextvars(slug=slug, stage="problems"):
        logger.info("problems_unpin_command_started")
        unpinned = mgr.storage.submissions_set_pin_status(slug, False)
        if unpinned:
            logger.info("submission_unpinned", slug=slug)
        else:
            logger.info("problems_unpin_command_skipped", reason="not_found")
        return unpinned


def _toggle_pin_one(mgr, slug: str) -> bool:
    """Toggle pin status for a submission. Returns True if now pinned."""
    with structlog.contextvars.bound_contextvars(slug=slug, stage="problems"):
        logger.info("problems_toggle_pin_command_started")
        new_pin_status = mgr.storage.submissions_toggle_pin(slug)
        if new_pin_status:  # Now pinned
            # If toggling to pinned and there's a pending submission part, remove it from cache
            if mgr.storage.is_part_pending(slug, "submission"):
                mgr.storage.remove_from_cache(slug)
                logger.info("pending_submission_part_removed_due_to_pin", slug=slug)
            logger.info("submission_pinned_via_toggle", slug=slug)
        else:  # Now unpinned
            logger.info("submission_unpinned_via_toggle", slug=slug)
        return new_pin_status


@problems.command("pin")
@click.argument("slug", required=False)
@click.option(
    "--all",
    "run_all",
    is_flag=True,
    help="Pin the stored submission for every slug that has one.",
)
@click.option("--skip-confirm", is_flag=True, help="Skip the confirmation prompt.")
def problems_pin_pin(slug: str | None, run_all: bool, skip_confirm: bool) -> None:
    """Pin the stored submission (code) for one or more problems.
    When pinned, the submission won't be fetched or looked for updates.
    Destructive — asks to confirm unless --skip-confirm.
    Omit both SLUG and --all to pick interactively instead — a searchable,
    multi-select prompt over every slug with a stored submission."""
    if slug and run_all:
        raise click.UsageError("Pass either SLUG or --all, not both.")

    mgr = get_manager()

    if slug:
        if not skip_confirm:
            click.confirm(
                f"Pin the stored submission for '{slug}'? This will prevent future updates.",
                abort=True,
            )
        if not _pin_one(mgr, slug):
            raise click.ClickException(f"'{slug}' has no stored submission.")
        click.echo(f"Pinned submission for '{slug}'.")
        return

    if run_all:
        slugs = [r.slug for r in mgr.storage.submissions_list_all() if r.slug]
        if not slugs:
            click.echo("Nothing to do — no stored submissions.")
            return
    else:
        slugs = _pick_submission_slugs(mgr)
        if not slugs:
            return

    if not skip_confirm:
        click.echo(f"About to pin {len(slugs)} submission(s): {', '.join(slugs)}")
        click.confirm("This cannot be undone. Continue?", abort=True)

    succeeded, failed = [], []
    for target_slug in slugs:
        if _pin_one(mgr, target_slug):
            click.echo(f"[done] pinned submission for {target_slug}")
            succeeded.append(target_slug)
        else:
            click.echo(f"[fail] {target_slug}: no stored submission")
            failed.append(target_slug)
    print_batch_summary(succeeded, failed)


@problems.command("unpin")
@click.argument("slug", required=False)
@click.option(
    "--all",
    "run_all",
    is_flag=True,
    help="Unpin the stored submission for every slug that has one.",
)
@click.option("--skip-confirm", is_flag=True, help="Skip the confirmation prompt.")
def problems_pin_unpin(slug: str | None, run_all: bool, skip_confirm: bool) -> None:
    """Unpin the stored submission (code) for one or more problems.
    When unpinned, the submission will be fetched and looked for updates again.
    Destructive — asks to confirm unless --skip-confirm.
    Omit both SLUG and --all to pick interactively instead — a searchable,
    multi-select prompt over every slug with a stored submission."""
    if slug and run_all:
        raise click.UsageError("Pass either SLUG or --all, not both.")

    mgr = get_manager()

    if slug:
        if not skip_confirm:
            click.confirm(
                f"Unpin the stored submission for '{slug}'? This will allow future updates.",
                abort=True,
            )
        if not _unpin_one(mgr, slug):
            raise click.ClickException(f"'{slug}' has no stored submission.")
        click.echo(f"Unpinned submission for '{slug}'.")
        return

    if run_all:
        slugs = [r.slug for r in mgr.storage.submissions_list_all() if r.slug]
        if not slugs:
            click.echo("Nothing to do — no stored submissions.")
            return
    else:
        slugs = _pick_submission_slugs(mgr)
        if not slugs:
            return

    if not skip_confirm:
        click.echo(f"About to unpin {len(slugs)} submission(s): {', '.join(slugs)}")
        click.confirm("This cannot be undone. Continue?", abort=True)

    succeeded, failed = [], []
    for target_slug in slugs:
        if _unpin_one(mgr, target_slug):
            click.echo(f"[done] unpinned submission for {target_slug}")
            succeeded.append(target_slug)
        else:
            click.echo(f"[fail] {target_slug}: no stored submission")
            failed.append(target_slug)
    print_batch_summary(succeeded, failed)


@problems.command("toggle")
@click.argument("slug", required=False)
@click.option(
    "--all",
    "run_all",
    is_flag=True,
    help="Toggle the pin status for every slug that has one.",
)
@click.option("--skip-confirm", is_flag=True, help="Skip the confirmation prompt.")
def problems_pin_toggle(slug: str | None, run_all: bool, skip_confirm: bool) -> None:
    """Toggle the pin status for the stored submission (code) for one or more problems.
    Toggling to pinned prevents future updates; toggling to unpinned allows updates again.
    Destructive — asks to confirm unless --skip-confirm.
    Omit both SLUG and --all to pick interactively instead — a searchable,
    multi-select prompt over every slug with a stored submission."""
    if slug and run_all:
        raise click.UsageError("Pass either SLUG or --all, not both.")

    mgr = get_manager()

    if slug:
        if not skip_confirm:
            click.confirm(
                f"Toggle pin status for '{slug}'? This will {'prevent' if not mgr.storage.submissions_get_pin_status(slug) else 'allow'} future updates.",
                abort=True,
            )
        _toggle_pin_one(mgr, slug)
        new_status = mgr.storage.submissions_get_pin_status(slug)
        click.echo(f"{'Pinned' if new_status else 'Unpinned'} submission for '{slug}'.")
        return

    if run_all:
        slugs = [r.slug for r in mgr.storage.submissions_list_all() if r.slug]
        if not slugs:
            click.echo("Nothing to do — no stored submissions.")
            return
    else:
        slugs = _pick_submission_slugs(mgr)
        if not slugs:
            return

    if not skip_confirm:
        click.echo(
            f"About to toggle pin status for {len(slugs)} submission(s): {', '.join(slugs)}"
        )
        click.confirm("This cannot be undone. Continue?", abort=True)

    succeeded, failed = [], []
    for target_slug in slugs:
        new_status = _toggle_pin_one(mgr, target_slug)
        if new_status is not None:  # Successfully toggled
            click.echo(
                f"[done] {'pinned' if new_status else 'unpinned'} submission for {target_slug}"
            )
            succeeded.append(target_slug)
        else:
            click.echo(f"[fail] {target_slug}: no stored submission")
            failed.append(target_slug)
    print_batch_summary(succeeded, failed)
