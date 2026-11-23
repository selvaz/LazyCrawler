from importlib.metadata import version

import lazycrawler


def test_version_matches_installed_distribution():
    assert lazycrawler.__version__ == version("lazycrawler")
