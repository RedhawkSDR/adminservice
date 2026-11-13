# Multi-stage image for REDHAWK adminservice, built through the rfnext
# ci-support docker.yml template (TARGETS selects the stage):
#   TARGETS=builder  -> builds the adminservice + python3-meld3 RPMs (/rpms)
#   (default)        -> rh-core-admin image (core stack + adminservice installed)
#
# BASE_IMAGE is the upstream core-framework image (…/core-framework:<ref>-core),
# passed as a BUILD_ARG from .gitlab-ci.images.yml.

ARG BASE_IMAGE=core-framework:develop-4.0-core

# ---- builder: build the adminservice RPM and its EL9-missing dep meld3 ----
FROM ${BASE_IMAGE} AS builder
RUN dnf -y install python3-setuptools python3-devel python3-rpm-macros rpm-build && dnf clean all
COPY . /src/adminservice
RUN set -eux; \
    TOP=/root/rpmbuild; mkdir -p "$TOP"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}; \
    # python3-meld3 (not packaged for EL9; fetched from PyPI) \
    meld3_ver="$(awk '/^Version:/{print $2; exit}' /src/adminservice/python3-meld3.spec)"; \
    curl -fsSL -o "$TOP/SOURCES/meld3-${meld3_ver}.tar.gz" \
         "https://files.pythonhosted.org/packages/source/m/meld3/meld3-${meld3_ver}.tar.gz"; \
    cp /src/adminservice/python3-meld3.spec "$TOP/SPECS/"; \
    rpmbuild --define "_topdir $TOP" --nodeps -ba "$TOP/SPECS/python3-meld3.spec"; \
    # redhawk-adminservice \
    ver="$(awk '/^Version:/{print $2; exit}' /src/adminservice/redhawk-adminservice.spec)"; \
    tarname="redhawk-adminservice-${ver}"; \
    cp -a /src/adminservice "/tmp/${tarname}"; rm -rf "/tmp/${tarname}/.git"; \
    tar czf "$TOP/SOURCES/${tarname}.tar.gz" -C /tmp "${tarname}"; \
    cp /src/adminservice/redhawk-adminservice.spec "$TOP/SPECS/"; \
    rpmbuild --define "_topdir $TOP" --define "_ossiehome /usr/local/redhawk/core" \
             --nodeps -ba "$TOP/SPECS/redhawk-adminservice.spec"; \
    mkdir -p /rpms; find "$TOP/RPMS" "$TOP/SRPMS" -name '*.rpm' -exec cp {} /rpms/ \; ; \
    ls -l /rpms
WORKDIR /rpms

# ---- runtime: add adminservice to the repo, snapshot, install ----
FROM ${BASE_IMAGE} AS runtime
ARG VERSION=4.0.4
COPY --from=builder /rpms /tmp/admin-rpms
RUN find /tmp/admin-rpms -name '*.rpm' ! -name '*.src.rpm' -exec cp -n {} /repo/${VERSION}/ \; && \
    createrepo_c --update /repo/${VERSION}
RUN tar czf /repo/rh-core-admin-${VERSION}.tar.gz -C /repo ${VERSION}
RUN dnf -y makecache && \
    dnf -y install redhawk-adminservice && \
    dnf clean all && \
    rm -rf /tmp/admin-rpms
WORKDIR /
