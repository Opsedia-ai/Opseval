# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
#
# The Opseval Bitbucket pipe: the Opseval CLI image (ADR-0038) plus the pipe's entrypoint.
# Until the CLI image is published, build it first and pass it in:
#
#   docker build -t opseval .                                    # in the Opseval repository
#   docker build -t opseval-pipe --build-arg OPSEVAL_IMAGE=opseval templates/bitbucket/pipe
#
# Pin the base image by digest when you publish the pipe.
#
# The pipe starts as root, unlike the CLI image: Bitbucket mounts the clone owned by root, so
# uid 10001 could not write the reports. pipe.sh uses root only to make the two report folders
# and give them to 10001, then runs everything else, Opseval included, as 10001 with setpriv
# (util-linux, part of the Debian base). Opseval never runs as root.
ARG OPSEVAL_IMAGE=ghcr.io/opsedia-ai/opseval:<version>
FROM ${OPSEVAL_IMAGE}
USER root
# Fails the build if setpriv is missing, rather than a pipe that could only run as root.
RUN setpriv --version
COPY --chmod=0755 pipe.sh /usr/local/bin/opseval-pipe
ENTRYPOINT ["/usr/local/bin/opseval-pipe"]
CMD []
