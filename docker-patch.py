#!/usr/bin/env python3
"""Backwards-compatible entry point. Use horus_patch.py instead.

Older docker-compose overrides run `python3 docker-patch.py <owrx_path>`;
this forwards that (and `--restore-frontend <path>`) to horus_patch.py.
"""
import os
import runpy
import sys

sys.argv[0] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "horus_patch.py")
runpy.run_path(sys.argv[0], run_name="__main__")
