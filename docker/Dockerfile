# Both the rootfs and the apt repository are immutable inputs.
FROM debian:bookworm-slim@sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018
ARG DEBIAN_FRONTEND=noninteractive
COPY scripts/install-deps.sh scripts/build-packages.txt /opt/routerfw/
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
      'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261105T000000Z bookworm main' \
      'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20261105T000000Z bookworm-security main' \
      > /etc/apt/sources.list \
    && bash /opt/routerfw/install-deps.sh \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 builder \
    && useradd -m -u 1000 -g builder -s /bin/bash builder
COPY docker/entrypoint.sh /usr/local/bin/routerfw-entrypoint
ENTRYPOINT ["/usr/local/bin/routerfw-entrypoint"]
CMD ["build"]
