##############################################################################
#
# Copyright (c) 2006-2015 Agendaless Consulting and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the BSD-like license at
# http://www.repoze.org/LICENSE.txt.  A copy of the license should accompany
# this distribution.  THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL
# EXPRESS OR IMPLIED WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND
# FITNESS FOR A PARTICULAR PURPOSE
#
##############################################################################

import os
import sys
from setuptools import setup

if sys.version_info[:2] < (2, 5):
    # meld3 1.0.0 dropped python 2.4 support
    # meld3 requires elementree on python 2.4 only
    requires = ['meld3 >= 0.6.5 , < 1.0.0', 'elementtree']
else:
    requires = ['meld3 >= 0.6.5']

ossiehome = os.getenv('OSSIEHOME')
homeSys = False
installArg = False
if 'install' in sys.argv:
    installArg = True
for arg in sys.argv:
    if '--home' in arg:
        homeSys = True
        
if not homeSys and ossiehome != None and installArg:
    sys.argv.append('--home='+ossiehome)
if not ('--old-and-unmanageable' in sys.argv) and installArg:
    sys.argv.append('--old-and-unmanageable')

README = """\
AdminService is a client/server system that allows its users to
control a number of REDHAWK processes on UNIX-like operating systems. 
It is based on Supervisor(http://supervisord.org)"""

here = os.path.abspath(os.path.dirname(__file__))
version_txt = os.path.join(here, 'adminservice/version.txt')
adminservice_version = open(version_txt).read().strip()

dist = setup(
    name='adminservice',
    version=adminservice_version,
    description="A system for controlling REDHAWK process state under UNIX",
    install_requires=requires,
    extras_require={'iterparse': ['cElementTree >= 1.0.2']},
    include_package_data=True,
    zip_safe=False,
    packages=['adminservice', 'adminservice/medusa'],
    entry_points={
        'console_scripts': [
        'adminserviced = adminservice.adminserviced:main',
        'rhadmin = adminservice.rhadmin:main',
        'echo_adminserviced_conf = adminservice.confecho:main',
        'pidproxy = adminservice.pidproxy:main',
        ],
    },
)
