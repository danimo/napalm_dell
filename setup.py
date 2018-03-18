"""setup.py file."""

import uuid

from setuptools import setup, find_packages
from pip.req import parse_requirements

__author__ = 'Daniel Molkentin <daniel@molkentin.de>'

install_reqs = parse_requirements('requirements.txt', session=uuid.uuid1())
reqs = [str(ir.req) for ir in install_reqs]

setup(
    name="napalm-dell",
    version="0.0.1",
    packages=find_packages(),
    author="Daniel Molkentin",
    author_email="daniel@molkentin.de",
    description="NAPALM driver for DellEMC switches running Dell PowerConnect OS",
    classifiers=[
        'Topic :: Utilities',
         'Programming Language :: Python',
         'Programming Language :: Python :: 2',
         'Programming Language :: Python :: 2.7',
         'Operating System :: POSIX :: Linux',
         'Operating System :: MacOS',
    ],
    include_package_data=True,
    install_requires=reqs,
)

