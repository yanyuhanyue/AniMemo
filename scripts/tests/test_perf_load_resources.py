import json
import unittest

from scripts.perf.resource_sampler import (
    DockerResourceSampler,
    ResourceConfig,
    ResourceSample,
    evaluate_resource_failures,
    parse_docker_stats,
    parse_postgres_connections,
    parse_redis_info,
    summarize_resources,
)


class PerformanceResourceSamplerTests(unittest.TestCase):
    def test_docker_stats_parser_normalizes_cpu_and_memory(self):
        payload = "\n".join(
            [
                json.dumps({"Name": "animemo-perf-api", "CPUPerc": "12.50%", "MemUsage": "128MiB / 1GiB"}),
                json.dumps({"Name": "animemo-perf-web", "CPUPerc": "0.25%", "MemUsage": "32.5MiB / 512MiB"}),
            ]
        )

        parsed = parse_docker_stats(payload)

        self.assertEqual(parsed["animemo-perf-api"]["cpu_percent"], 12.5)
        self.assertEqual(parsed["animemo-perf-api"]["memory_bytes"], 128 * 1024 * 1024)
        self.assertEqual(parsed["animemo-perf-web"]["memory_bytes"], int(32.5 * 1024 * 1024))

    def test_database_and_redis_parsers_use_read_only_metric_output(self):
        self.assertEqual(parse_postgres_connections("17|100\n"), {"connections": 17, "max_connections": 100})
        self.assertEqual(
            parse_redis_info("used_memory:4096\r\nused_memory_human:4K\r\n", "23\n"),
            {"used_memory_bytes": 4096, "keys": 23},
        )

    def test_sampler_targets_only_explicit_isolated_containers_with_read_only_commands(self):
        calls = []

        def run_command(command):
            calls.append(tuple(command))
            if command[1] == "stats":
                return "\n".join(
                    json.dumps({"Name": name, "CPUPerc": "1%", "MemUsage": "64MiB / 1GiB"})
                    for name in (
                        "animemo-perf-123-api",
                        "animemo-perf-123-web",
                        "animemo-perf-123-postgres",
                        "animemo-perf-123-redis",
                    )
                )
            if "psql" in command:
                return "4|100\n"
            if "INFO" in command:
                return "used_memory:8192\n"
            return "9\n"

        sampler = DockerResourceSampler(
            ResourceConfig(
                compose_project="animemo-perf-123",
                postgres_user="animemo_perf",
                postgres_database="animemo_perf",
                confirm_isolated=True,
            ),
            run_command=run_command,
        )

        sample = sampler.sample()

        self.assertEqual(sample.database_connections, 4)
        self.assertEqual(sample.redis_keys, 9)
        self.assertEqual(set(sample.containers), {"api", "web", "postgres", "redis"})
        flattened = " ".join(part for call in calls for part in call).lower()
        for destructive in (" rm ", " prune ", " flushall ", " flushdb ", " delete ", " update "):
            self.assertNotIn(destructive, f" {flattened} ")

    def test_sampler_rejects_unconfirmed_or_production_compose_names(self):
        with self.assertRaises(ValueError):
            ResourceConfig(
                compose_project="animemo-perf-123",
                postgres_user="animemo_perf",
                postgres_database="animemo_perf",
            )
        with self.assertRaises(ValueError):
            ResourceConfig(
                compose_project="animemo",
                postgres_user="animemo",
                postgres_database="animemo",
                confirm_isolated=True,
            )

    def test_resource_failures_require_exhaustion_or_sustained_absolute_growth(self):
        stable = [
            ResourceSample.for_test(api_memory=100, redis_memory=100, redis_keys=10, db_connections=10),
            ResourceSample.for_test(api_memory=120, redis_memory=120, redis_keys=12, db_connections=12),
            ResourceSample.for_test(api_memory=110, redis_memory=110, redis_keys=11, db_connections=11),
            ResourceSample.for_test(api_memory=115, redis_memory=115, redis_keys=12, db_connections=10),
        ]
        self.assertEqual(
            evaluate_resource_failures(
                stable,
                memory_growth_limit_bytes=20,
                redis_memory_growth_limit_bytes=20,
                redis_key_growth_limit=2,
            ),
            [],
        )

        runaway = [
            ResourceSample.for_test(api_memory=100, redis_memory=100, redis_keys=10, db_connections=95, max_connections=100),
            ResourceSample.for_test(api_memory=110, redis_memory=110, redis_keys=11, db_connections=98, max_connections=100),
            ResourceSample.for_test(api_memory=120, redis_memory=120, redis_keys=12, db_connections=99, max_connections=100),
            ResourceSample.for_test(api_memory=130, redis_memory=130, redis_keys=13, db_connections=100, max_connections=100),
        ]
        failures = evaluate_resource_failures(
            runaway,
            memory_growth_limit_bytes=30,
            redis_memory_growth_limit_bytes=30,
            redis_key_growth_limit=3,
        )
        self.assertIn("database_connection_exhaustion", failures)
        self.assertIn("api_memory_runaway_growth", failures)
        self.assertIn("redis_memory_runaway_growth", failures)
        self.assertIn("redis_key_runaway_growth", failures)

    def test_resource_summary_records_start_end_peaks_and_growth(self):
        samples = [
            ResourceSample.for_test(api_memory=100, api_cpu=1.0, redis_memory=1000, redis_keys=10, db_connections=3),
            ResourceSample.for_test(api_memory=140, api_cpu=8.0, redis_memory=1200, redis_keys=12, db_connections=7),
        ]

        summary = summarize_resources(samples)

        self.assertEqual(summary["containers"]["api"]["memory_growth_bytes"], 40)
        self.assertEqual(summary["containers"]["api"]["peak_cpu_percent"], 8.0)
        self.assertEqual(summary["database"]["peak_connections"], 7)
        self.assertEqual(summary["redis"]["key_growth"], 2)


if __name__ == "__main__":
    unittest.main()
