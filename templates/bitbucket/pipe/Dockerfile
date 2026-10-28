# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0
#
# The Opseval Bitbucket pipe: the Opseval CLI image (ADR-0038) plus the pipe's entrypoint.
# Until the CLI image is published, build it first and pass it in:
#
#   docker build -t opseval .                                    # in the Opseval repository
#   docker build -t opseval-pipe --build-arg OPSEVAL_IMAGE=opseval templates/bitbucket/pipe
#
# Pin the base image by digest when you publish the pipe. It keeps the CLI image's user
# (uid 10001): the pipe never runs as root.
ARG OPSEVAL_IMAGE=ghcr.io/opsedia-ai/opseval:<version>
FROM ${OPSEVAL_IMAGE}
COPY --chmod=0755 pipe.sh /usr/local/bin/opseval-pipe
ENTRYPOINT ["/usr/local/bin/opseval-pipe"]
CMD []
