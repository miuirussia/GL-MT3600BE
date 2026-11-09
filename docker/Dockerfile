FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
COPY scripts/install-deps.sh scripts/build-packages.txt /opt/routerfw/
RUN bash /opt/routerfw/install-deps.sh \
    && rm -rf /var/lib/apt/lists/* \
    && groupmod -n builder ubuntu \
    && usermod -l builder -d /home/builder -m -s /bin/bash ubuntu
COPY docker/entrypoint.sh /usr/local/bin/routerfw-entrypoint
ENTRYPOINT ["/usr/local/bin/routerfw-entrypoint"]
CMD ["build"]
