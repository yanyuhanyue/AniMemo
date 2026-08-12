"""Run the AniMemo v1.0 backend/PostgreSQL performance baseline."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from performance.contract import REQUIRED_DATABASE_VENDOR
from performance.probe import run_backend_probes, write_probe_report
from performance.seed import dataset_shape, seed_backend_performance_data


class Command(BaseCommand):
    help = "Seed and measure AniMemo backend API/query performance in an isolated database."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", choices=("small", "medium", "large"), default="small")
        parser.add_argument("--output", required=True)
        parser.add_argument("--explain", action="store_true")
        parser.add_argument(
            "--allow-sqlite-query-shape",
            action="store_true",
            help="Run non-authoritative query-count/payload checks only; latency is not reported.",
        )

    def handle(self, *args, **options):
        vendor = connection.vendor
        authoritative = vendor == REQUIRED_DATABASE_VENDOR
        if not authoritative and not options["allow_sqlite_query_shape"]:
            raise CommandError(
                "Authoritative backend performance measurement requires PostgreSQL; "
                f"current database vendor is {vendor}."
            )
        if options["explain"] and not authoritative:
            raise CommandError("--explain requires PostgreSQL and uses EXPLAIN (ANALYZE, BUFFERS).")

        shape = dataset_shape(options["dataset"])
        self.stdout.write(
            f"Seeding {shape.name}: entries={shape.journal_entries}, "
            f"users={shape.supporting_users}, plugins={shape.plugins}, "
            f"watch_history={shape.watch_history_records}"
        )
        seed_result = seed_backend_performance_data(options["dataset"], reset=True)
        report = run_backend_probes(
            seed_result,
            authoritative=authoritative,
            explain=options["explain"],
        )
        output_path = Path(options["output"]).resolve()
        write_probe_report(report, output_path)
        self.stdout.write(self.style.SUCCESS(f"Backend performance evidence written to {output_path}"))
        if not authoritative:
            self.stdout.write(
                self.style.WARNING(
                    "SQLite query-shape mode is auxiliary only; PostgreSQL latency and EXPLAIN remain NOT RUN."
                )
            )
