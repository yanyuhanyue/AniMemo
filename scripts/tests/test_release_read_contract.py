"""Include production read and frozen occupancy regressions in existing CI."""
import unittest


def load_tests(loader, tests, pattern):
    for name in ('release.test_github_release_read', 'release.test_frozen_occupancy'):
        tests.addTests(loader.loadTestsFromName(name))
    return tests
