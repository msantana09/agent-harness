# A machine with nothing on it but Python, Node, git, Claude Code and this repo, installed one of
# the two ways the README gives: as a plugin (the default target) or from a clone (target `clone`).
# run.sh builds either from a copy of the committed tree.
FROM node:24.22.1-bookworm-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c AS node

FROM python:3.14.9-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae AS base
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install --global @anthropic-ai/claude-code@2.1.316 \
    && apt-get update && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home dev && install -d -o dev /login /out /home/dev/shop
USER dev
WORKDIR /home/dev/shop
ENV CLAUDE_CONFIG_DIR=/home/dev/.claude

COPY --chown=dev . /home/dev/agent-harness

FROM base AS clone
RUN cd /home/dev && agent-harness/tests/container/clone_install.sh
ENTRYPOINT ["/home/dev/agent-harness/tests/container/clone_inside.sh"]

FROM base AS plugin
RUN claude plugin marketplace add /home/dev/agent-harness \
    && claude plugin install agent-harness@msantana09
ENTRYPOINT ["/home/dev/agent-harness/tests/container/inside.sh"]
