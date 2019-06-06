#
# This file is protected by Copyright. Please refer to the COPYRIGHT file
# distributed with this source distribution.
#
# This file is part of REDHAWK core.
#
# REDHAWK core is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# REDHAWK core is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see http://www.gnu.org/licenses/.
#
%if 0%{?fedora} >= 17 || 0%{?rhel} >=7
%global with_systemd 1
%endif
%{!?python_sitelib: %define python_sitelib %(%{__python} -c "from distutils.sysconfig import get_python_lib; print get_python_lib()")}
%{!?_ossiehome:  %define _ossiehome  /usr/local/redhawk/core}
%define _prefix %{_ossiehome}
Prefix:         %{_prefix}

Name:           redhawk-adminservice
Version:        2.2.3
Release:        1%{?dist}
Summary:        Redhawk Admin Service

Group:          Applications/Engineering
License:        LGPLv3+
URL:            http://redhawksdr.org/
Source:         %{name}-%{version}.tar.gz
Vendor:         REDHAWK

BuildArch:      noarch

Requires:       python
Requires:       redhawk >= 2.0
Requires(pre):  redhawk >= 2.0
Requires:       python-meld3
Requires:       python-tornado
Requires:       python-zmq

BuildRequires:  python-setuptools
BuildRequires:  python-devel >= 2.4
%if 0%{?with_systemd}
%{?systemd_requires}
BuildRequires:   systemd
Requires:        systemd
Requires(pre):   systemd
%else
Requires:        initscripts
Requires(pre):   initscripts
Requires:        chkconfig
%endif

# Turn off the brp-python-bytecompile script; our setup.py does byte compilation
# (From https://fedoraproject.org/wiki/Packaging:Python#Bytecompiling_with_the_correct_python_version)
%global __os_install_post %(echo '%{__os_install_post}' | sed -e 's!/usr/lib[^[:space:]]*/brp-python-bytecompile[[:space:]].*$!!g')

%description
REDHAWK Admin Service
 * Commit: __REVISION__
 * Source Date/Time: __DATETIME__


%prep
%if 0%{?_localbuild}
%setup -q -n adminservice
%else
%setup -q
%endif


%build
%{__python} setup.py build


%install
rm -rf $RPM_BUILD_ROOT
%{__python} setup.py install --skip-build -O1 --home=%{_prefix} --root=%{buildroot}
install -d -m 0775 $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/domains.d
install -d -m 0775 $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/extras.d
install -d -m 0775 $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/init.d
install -d -m 0775 $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/nodes.d
install -d -m 0775 $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/waveforms.d
install -d -m 0775 $RPM_BUILD_ROOT%{_localstatedir}/log/redhawk
install -d -m 0775 $RPM_BUILD_ROOT%{_localstatedir}/log/redhawk/device-mgrs
install -d -m 0775 $RPM_BUILD_ROOT%{_localstatedir}/log/redhawk/domain-mgrs
install -d -m 0775 $RPM_BUILD_ROOT%{_localstatedir}/log/redhawk/waveforms
%{__cp} -r etc $RPM_BUILD_ROOT/
%{__mv} $RPM_BUILD_ROOT/%{_sysconfdir}/redhawk/cron.d $RPM_BUILD_ROOT/%{_sysconfdir}/cron.d
%{__cp} -r bin/* $RPM_BUILD_ROOT/%{_bindir}
%if 0%{?with_systemd}
%{__mkdir_p} $RPM_BUILD_ROOT%{_unitdir}
%{__mv} $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/systemd/redhawk-adminservice-setup $RPM_BUILD_ROOT%{_bindir}
%{__mv} $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/systemd/redhawk-adminservice.service $RPM_BUILD_ROOT%{_unitdir}
%{__mv} $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/systemd/redhawk-adminservice-setup.service $RPM_BUILD_ROOT%{_unitdir}
%else
rm -f $RPM_BUILD_ROOT/%{_bindir}/adminserviced-start
rm -f $RPM_BUILD_ROOT/%{_bindir}/adminserviced-stop
install -d -m 0775 $RPM_BUILD_ROOT%{_localstatedir}/run/redhawk
install -d -m 0775 $RPM_BUILD_ROOT%{_localstatedir}/run/redhawk/device-mgrs
install -d -m 0775 $RPM_BUILD_ROOT%{_localstatedir}/run/redhawk/domain-mgrs
install -d -m 0775 $RPM_BUILD_ROOT%{_localstatedir}/run/redhawk/waveforms
%endif
rm -r $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/systemd
echo "ENABLED=1" > $RPM_BUILD_ROOT%{_sysconfdir}/redhawk/rh.cond.cfg


%clean
rm -rf $RPM_BUILD_ROOT


%files
%defattr(-,root,root,-)
%{_bindir}/adminserviced
%{_bindir}/rhadmin
%config %attr(660,root,redhawk) %{_sysconfdir}/redhawk/adminserviced.conf
%config %attr(664,root,redhawk) %{_sysconfdir}/redhawk/rh.cond.cfg
%dir %attr(775,root,redhawk) %{_sysconfdir}/redhawk/domains.d
%dir %attr(775,root,redhawk) %{_sysconfdir}/redhawk/extras.d
%dir %attr(775,root,redhawk) %{_sysconfdir}/redhawk/nodes.d
%dir %attr(775,root,redhawk) %{_sysconfdir}/redhawk/waveforms.d
%dir %attr(775,root,redhawk) %{_sysconfdir}/redhawk/init.d
%config %attr(664,root,redhawk) %{_sysconfdir}/redhawk/init.d/adminserviced.defaults
%config %attr(664,root,redhawk) %{_sysconfdir}/redhawk/init.d/domain.defaults
%config %attr(664,root,redhawk) %{_sysconfdir}/redhawk/init.d/node.defaults
%config %attr(664,root,redhawk) %{_sysconfdir}/redhawk/init.d/waveform.defaults
%attr(775,root,redhawk) %{_sysconfdir}/redhawk/init.d/redhawk-adminservice
%attr(775,root,redhawk) %{_sysconfdir}/redhawk/init.d/redhawk-wf-control
%attr(644,root,redhawk) %{_sysconfdir}/cron.d/redhawk
%dir %attr(775,root,redhawk) %{_sysconfdir}/redhawk/logging
%attr(664,root,redhawk) %{_sysconfdir}/redhawk/logging/default.logging.properties
%attr(664,root,redhawk) %{_sysconfdir}/redhawk/logging/example.logging.properties
%attr(664,root,redhawk) %{_sysconfdir}/redhawk/logging/logrotate.redhawk
%dir %attr(775,root,redhawk) %{_sysconfdir}/redhawk/security
%dir %attr(775,root,redhawk) %{_sysconfdir}/redhawk/security/limits.d
%attr(664,root,redhawk) %{_sysconfdir}/redhawk/security/limits.d/99-redhawk-limits.conf
%dir %attr(775,root,redhawk) %{_sysconfdir}/redhawk/sysctl.d
%attr(664,root,redhawk) %{_sysconfdir}/redhawk/sysctl.d/sysctl.conf
%dir %attr(775,root,redhawk) %{_localstatedir}/log/redhawk
%dir %attr(775,root,redhawk) %{_localstatedir}/log/redhawk/device-mgrs
%dir %attr(775,root,redhawk) %{_localstatedir}/log/redhawk/domain-mgrs
%dir %attr(775,root,redhawk) %{_localstatedir}/log/redhawk/waveforms
%{_prefix}/lib/python/adminservice
%if 0%{?rhel} >= 6
%{_prefix}/lib/python/adminservice-%{version}-py%{python_version}.egg-info
%endif
%if 0%{?with_systemd}
%{_bindir}/adminserviced-start
%{_bindir}/adminserviced-stop
%{_bindir}/redhawk-adminservice-setup
%{_unitdir}/redhawk-adminservice.service
%{_unitdir}/redhawk-adminservice-setup.service
%else
%dir %attr(775,root,redhawk) %{_localstatedir}/run/redhawk
%dir %attr(775,root,redhawk) %{_localstatedir}/run/redhawk/device-mgrs
%dir %attr(775,root,redhawk) %{_localstatedir}/run/redhawk/domain-mgrs
%dir %attr(775,root,redhawk) %{_localstatedir}/run/redhawk/waveforms
%endif

%post
%if 0%{?with_systemd}
%systemd_post redhawk-adminservice-setup.service
%systemd_post redhawk-adminservice.service

systemctl reload crond > /dev/null 2>&1 || :
%else
ln -s %{_sysconfdir}/redhawk/init.d/redhawk-adminservice %{_initddir}
/sbin/chkconfig --add redhawk-adminservice

service crond reload > /dev/null 2>&1 || :
%endif

%preun
%if 0%{?with_systemd}
%systemd_preun redhawk-adminservice.service
%systemd_preun redhawk-adminservice-setup.service
%else
/sbin/service redhawk-adminservice stop > /dev/null 2>&1 || :
/sbin/chkconfig --del redhawk-adminservice  > /dev/null 2>&1 || :
%endif

%postun
%if 0%{?with_systemd}
%systemd_postun_with_restart redhawk-adminservice.service
%systemd_postun_with_restart redhawk-adminservice-setup.service

systemctl reload crond  > /dev/null 2>&1 || :
%else
[ -h %{_initddir}/redhawk-adminservice ] && rm -f %{_initddir}/redhawk-adminservice

/sbin/service crond reload  > /dev/null 2>&1 || :
%endif

%changelog
