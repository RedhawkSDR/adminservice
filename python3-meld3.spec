%global srcname meld3

Name:           python3-meld3
Version:        2.0.1
Release:        1%{?dist}
Summary:        HTML/XML templating engine required by redhawk-adminservice

License:        ZPL-2.1
URL:            https://github.com/Supervisor/meld3
# meld3 is a runtime dependency of redhawk-adminservice (a Supervisor
# derivative) and is not packaged in the EL9 repositories, so it is built here.
Source0:        https://files.pythonhosted.org/packages/source/m/%{srcname}/%{srcname}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-rpm-macros
Requires:       python3

%description
meld3 is an HTML/XML templating system for Python, packaged for EL9 because it
is unavailable in the distribution repositories.

%prep
%autosetup -n %{srcname}-%{version}

%build
%py3_build

%install
%py3_install

%files
%{python3_sitelib}/%{srcname}/
%{python3_sitelib}/%{srcname}-%{version}-py%{python3_version}.egg-info

%changelog
* Sun Oct 19 2025 REDHAWK <redhawk@redhawksdr.org> - 2.0.1-1
- Package meld3 for EL9 to satisfy redhawk-adminservice.
