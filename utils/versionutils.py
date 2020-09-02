#!/usr/bin/env python
# coding=utf-8

"""
version utils
"""

from __future__ import print_function, unicode_literals


def get_version_from_init_file():
    """parse version info from __init__.py file.

    Return the version string.

    """
    with open("m3u8downloader/__init__.py", "r") as f:
        for line in f:
            if "__version__" in line:
                return line.split('"')[1]
    raise Exception("__version__ not found in m3u8downloader/__init__.py")


def get_version_from_setup_file():
    """parse version info from setup.py file.

    Return the version string.

    """
    with open("setup.py", "r") as f:
        for line in f:
            if "version=" in line:
                return line.split("=")[1][1:-3]
    raise Exception("version not found in setup.py")
