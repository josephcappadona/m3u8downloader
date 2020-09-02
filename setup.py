#!/usr/bin/env python
# coding=utf-8

"""
python distribute file
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals, with_statement)

from setuptools import setup, find_packages
from utils.versionutils import get_version_from_init_file


def requirements_file_to_list(fn="requirements.txt"):
    """read a requirements file and create a list that can be used in setup.

    """
    with open(fn, 'r') as f:
        return [x.rstrip() for x in list(f) if x and not x.startswith('#')]


setup(
    name="m3u8downloader",
    version=get_version_from_init_file(),
    packages=find_packages(exclude=("utils",)),
    install_requires=requirements_file_to_list(),
    entry_points={
        'console_scripts': [
            'downloadm3u8 = m3u8downloader.main:main',
        ]
    },
    package_data={
        'm3u8downloader': ['logger.conf']
    },
    author="Yuanle Song",
    author_email="sylecn@gmail.com",
    maintainer="Yuanle Song",
    maintainer_email="sylecn@gmail.com",
    description="a cli program to download video at m3u8 url",
    long_description=open('README.rst').read(),
    long_description_content_type='text/x-rst',
    license="GPLv2+",
    url="https://pypi.org/project/m3u8downloader/",
    classifiers=[
        'Development Status :: 4 - Beta',
        'License :: OSI Approved :: GNU General Public License (GPL)',
        'License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)',
        'Programming Language :: Python :: 3.5',
    ]
)
