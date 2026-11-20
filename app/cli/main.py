"""``veda`` - the command line the office actually runs.

Every command here is safe to run at any time:

* nothing dials a number,
* nothing calls a provider,
* nothing prints a secret (``check-config`` masks every credential), and
* ``import`` previews by default and only writes with an explicit ``--apply``.

The pieces that *do* need live credentials - synthesising audio, placing a call
- are reported as unconfigured rather than attempted. That is deliberate: a
command that half-works with a missing key is worse than one that says so.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence

from ..config.settings import Stage1Settings, load_stage1_settings
from ..domain.phone import is_valid_phone, normalise_phone
from ..ingestion import service as ingestion
from ..integrations.database import audio_repo, call_repo, import_repo, migrate, repositories
from ..integrations.database.engine import Database, database_from_url

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_REFUSED = 2


# --------------------------------------------------------------------- helpers
def _database(settings: Stage1Settings) -> Database:
    return database_from_url(settings.database_url)


def _print_table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> None:
    table = [list(headers)] + [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    widths = [max(len(row[index]) for row in table) for index in range(len(headers))]
    for index, row in enumerate(table):
        print("  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)))
        if index == 0:
            print("  ".join("-" * width for width in widths))


def _settings(args: argparse.Namespace) -> Stage1Settings:
    return load_stage1_settings(require_secrets=False, require_school=False)


# -------------------------------------------------------------------- commands
async def cmd_check_config(args: argparse.Namespace) -> int:
    settings = _settings(args)
    summary = settings.safe_summary()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    missing = [key for key, value in summary.items() if value == "missing"]
    if missing:
        print()
        print("not configured: " + ", ".join(missing))
        print("(see .env.example; nothing is called until these are set)")
        return EXIT_PROBLEM
    print("\nconfiguration looks complete. No provider was contacted.")
    return EXIT_OK


async def cmd_import(args: argparse.Namespace) -> int:
    from .picker import PickerCancelled, pick_workbook

    settings = _settings(args)
    try:
        path = pick_workbook(Path(args.file) if args.file else None)
    except PickerCancelled as exc:
        print(f"import cancelled: {exc}")
        return EXIT_REFUSED

    apply = bool(args.apply)
    if apply and not args.yes:
        print(f"About to write rows from {path.name} into the database.")
        answer = input("Type 'apply' to continue: ").strip().lower()
        if answer != "apply":
            print("cancelled; nothing was written")
            return EXIT_REFUSED

    async with _database(settings).session() as session:
        inspected, summary = await ingestion.import_file(
            session, path, settings=settings, school_id=settings.school_id, dry_run=not apply
        )
        await session.commit()

    print(ingestion.render_report_text(inspected))
    if inspected.repeat_upload:
        print(f"\nThis exact file was already imported (batch {inspected.existing_batch_id}).")
        return EXIT_OK
    if summary is not None:
        print(
            f"\napplied: {summary.students_created} created, "
            f"{summary.students_updated} updated, {summary.tasks_created} new tasks, "
            f"{summary.tasks_existing} already present, {summary.tasks_blocked} blocked"
        )
    else:
        print("\ndry run: nothing was written. Re-run with --apply to import.")
    return EXIT_OK


async def cmd_preview(args: argparse.Namespace) -> int:
    settings = _settings(args)
    async with _database(settings).session() as session:
        rows = await repositories.preview_tasks(session, limit=args.limit)
        counts = await repositories.task_counts(session)
    if not rows:
        print("nothing is ready to call")
        return EXIT_OK
    _print_table(
        [
            (
                row.task_id,
                row.student_name,
                row.parent_phone_masked,
                row.status,
                row.attempts,
            )
            for row in rows
        ],
        ["task", "student", "phone", "status", "attempts"],
    )
    print("\n" + json.dumps(counts, indent=2))
    print("\nthis is a preview; nothing was dialled")
    return EXIT_OK


async def cmd_export(args: argparse.Namespace) -> int:
    settings = _settings(args)
    out_dir = Path(args.out or settings.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / f"veda_export_{datetime.now():%Y%m%d_%H%M%S}.csv"

    async with _database(settings).session() as session:
        rows = await call_repo.export_rows(session, limit=args.limit)

    if not rows:
        print("no completed calls to export yet")
        return EXIT_OK

    fieldnames = [field.name for field in dataclasses.fields(rows[0])]
    with open(destination, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            record = dataclasses.asdict(row)
            record["promised_date"] = str(record["promised_date"] or "")
            record["recorded_at"] = str(record["recorded_at"] or "")
            writer.writerow(record)
    print(f"wrote {len(rows)} rows to {destination}")
    print("phone numbers in this file are masked")
    return EXIT_OK


async def cmd_stats(args: argparse.Namespace) -> int:
    settings = _settings(args)
    async with _database(settings).session() as session:
        outcomes = await call_repo.outcome_counts(session)
        reasons = await call_repo.reason_counts(session)
        tasks = await repositories.task_counts(session)
        batches = await import_repo.list_batches(session, limit=5)
        cache = await audio_repo.summary(session)

    print("outcomes")
    _print_table(sorted(outcomes.items(), key=lambda item: -item[1]) or [("none", 0)],
                 ["outcome", "calls"])
    print("\nreasons")
    _print_table(sorted(reasons.items(), key=lambda item: -item[1]) or [("none", 0)],
                 ["reason", "calls"])
    print("\ntasks")
    _print_table(sorted(tasks.items()) or [("none", 0)], ["status", "count"])
    print("\nrecent imports")
    _print_table(
        [(batch.id, batch.status, batch.created_at) for batch in batches],
        ["batch", "status", "created"],
    )
    print("\naudio cache")
    print("  " + json.dumps(cache))
    return EXIT_OK


async def cmd_migrate(args: argparse.Namespace) -> int:
    settings = _settings(args)
    revision = await asyncio.to_thread(migrate.apply_migrations_sync, settings)
    print(f"database is at revision {revision}")
    print(f"({settings.redacted_database_url})")
    return EXIT_OK


async def cmd_numbers(args: argparse.Namespace) -> int:
    settings = _settings(args)
    async with _database(settings).session() as session:
        if args.number_command == "add":
            phone, _ = normalise_phone(args.phone)
            if not phone or not is_valid_phone(phone):
                print(f"{args.phone!r} is not an Indian mobile number")
                return EXIT_REFUSED
            added = await repositories.add_do_not_call(
                session, phone=phone, reason=args.reason or "operator request"
            )
            await session.commit()
            print("added to the do-not-call list" if added else "that number is already blocked")
        elif args.number_command == "remove":
            phone, _ = normalise_phone(args.phone)
            removed = await repositories.remove_do_not_call(session, phone or args.phone)
            await session.commit()
            print("removed" if removed else "that number was not on the list")
        else:
            rows = await repositories.list_do_not_call(session, limit=args.limit)
            _print_table(
                [(row.phone, row.reason, row.created_at) for row in rows],
                ["phone", "reason", "added"],
            )
    return EXIT_OK


async def cmd_validate_audio(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from ..cache.audio_cache import AudioCache
    from ..cache.response_cache import ResponseCache

    responses = ResponseCache()
    cache = AudioCache(
        root=settings.audio_cache_dir,
        responses=responses,
        voice=getattr(settings.base, "sarvam_tts_voice", ""),
        model=getattr(settings.base, "sarvam_tts_model", ""),
    )
    keys = cache.cacheable_keys
    print("phrases allowed in the audio cache (section 13.7):")
    for key in keys:
        print(f"  - {key}")

    configured = bool(getattr(settings.base, "sarvam_api_key", ""))
    if not configured:
        print(
            "\nSarvam is not configured, so no audio was synthesised here. "
            "Run 'veda validate-audio' again after setting SARVAM_API_KEY: it will "
            "build each phrase, play nothing, and verify the result is silent-free "
            "before the first call."
        )
        return EXIT_PROBLEM

    async with _database(settings).session() as session:
        summary = await audio_repo.summary(session)
    print("\n" + json.dumps(summary))
    return EXIT_OK


async def cmd_doctor(args: argparse.Namespace) -> int:
    """A readiness report. Says what is fine, what is missing, what needs a call."""
    settings = _settings(args)
    problems: List[str] = []

    database = _database(settings)
    try:
        healthy = await database.healthy()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        healthy = False
        problems.append(f"database unreachable ({type(exc).__name__})")
    else:
        if not healthy:
            problems.append("database did not answer a trivial query")

    if healthy:
        try:
            revision = await asyncio.to_thread(migrate.current_revision, settings)
        except Exception as exc:  # noqa: BLE001
            revision = None
            problems.append(f"could not read the migration revision ({type(exc).__name__})")
        if revision is None:
            problems.append("the database has no schema yet; run 'veda migrate'")
        else:
            print(f"database: ok (revision {revision})")

    summary = settings.safe_summary()
    missing = [key for key, value in summary.items() if value == "missing"]
    if missing:
        print("configuration: " + ", ".join(missing) + " are not set")
        if any(name.startswith("exotel_") for name in missing):
            problems.append("Exotel is not configured, so no call can be placed")
        if "sarvam_api_key" in missing:
            problems.append("Sarvam is not configured, so no speech can be produced")

    print("\nthese checks need live credentials and were not run:")
    print("  - placing one real call to a number you control")
    print("  - verifying an Exotel status callback from your tunnel")
    print("  - synthesising and playing one cached phrase")
    print("  - recognising a spoken date end to end")

    if problems:
        print("\nblocking problems:")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_PROBLEM
    print("\nno blocking problems found locally")
    return EXIT_OK


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veda",
        description="Fee reminder calls for the school office. Nothing here dials a number.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-config", help="show the configuration with every secret masked")

    importer = sub.add_parser("import", help="import a fee workbook (previews by default)")
    importer.add_argument("--file", help="workbook to import; omit to open a file picker")
    importer.add_argument("--apply", action="store_true", help="write the rows (default: preview)")
    importer.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    preview = sub.add_parser("preview", help="show what a calling run would dial")
    preview.add_argument("--limit", type=int, default=25)

    export = sub.add_parser("export", help="write the staff CSV export")
    export.add_argument("--out", help="output folder (default: the configured export folder)")
    export.add_argument("--limit", type=int, default=5000)

    sub.add_parser("stats", help="outcomes, reasons, tasks, imports and cache counts")

    migrate_parser = sub.add_parser("migrate", help="apply database migrations")
    migrate_parser.add_argument("--url", help="override the database URL for this run")

    numbers = sub.add_parser("numbers", help="manage the do-not-call list")
    number_sub = numbers.add_subparsers(dest="number_command", required=True)
    for name, help_text in (("add", "block a number"), ("remove", "unblock a number")):
        child = number_sub.add_parser(name, help=help_text)
        child.add_argument("phone")
        if name == "add":
            child.add_argument("--reason", default="")
    listing = number_sub.add_parser("list", help="show blocked numbers")
    listing.add_argument("--limit", type=int, default=50)

    sub.add_parser("validate-audio", help="list the cacheable phrases and build them if configured")
    sub.add_parser("doctor", help="readiness report: what is fine, what is missing, what needs a call")

    return parser


COMMANDS = {
    "check-config": cmd_check_config,
    "import": cmd_import,
    "preview": cmd_preview,
    "export": cmd_export,
    "stats": cmd_stats,
    "migrate": cmd_migrate,
    "numbers": cmd_numbers,
    "validate-audio": cmd_validate_audio,
    "doctor": cmd_doctor,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS[args.command]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\ninterrupted")
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - a CLI must explain, not traceback
        print(f"{args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_PROBLEM


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
