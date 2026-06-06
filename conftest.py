# -*- coding: utf-8 -*-
"""
pytest bootstrap: put LazyCrawler (+ LazyBridge) on sys.path and load .env so
`pytest` finds the package and API keys without any PYTHONPATH juggling.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup_paths  # noqa: E402,F401  (side effects: sys.path + .env)
