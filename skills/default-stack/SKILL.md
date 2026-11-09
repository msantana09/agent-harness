---
name: default-stack
description: The default stack for every new project and prototype Claude starts - languages, frameworks, database, and test and lint tools for a web app, a Python tool or pipeline, or an LLM app. Use before writing the first manifest (pyproject.toml, package.json, compose file) of a new build, and when a language or framework comes up in the new-build interview. Skip it for an existing project (its manifest is its stack), a prototype whose point is trying a technology, and a phone app. A language, framework or tool the user names replaces only that piece; the rest of the stack still applies.
---

# Default Stack

Every new build starts from one stack, run in production and across many repos, whose tools are
known to work together. A different language (Rust, Go) is a conversation with the user, not a
default.

## When it applies

- **Applies**: a new repo or prototype that is a web app (UI and API), a Python tool or pipeline,
  or an LLM or agent app, side projects included.
- **Does not apply**:
  - An existing project: its manifest and `CLAUDE.md` are its stack.
  - A prototype whose moment is a technology (Keycloak, Trino, Kubernetes): it runs that
    technology.
  - A phone app.
  - The product tier: hosting, cloud, tenancy, CI and deploy gates are decided when a project
    becomes a product.
- **Deviating**: a swap of any piece below (another framework, database or test tool) goes to
  the user through `AskUserQuestion` first, unless they named it. A library for a
  need the list does not cover is an ordinary dependency choice.

## The stack

**Every project**

- Python 3.14 or newer, managed by `uv`: `uv.lock` committed, `[tool.uv] exclude-newer = "7 days"`.
- pydantic for data shapes.
- pytest; ruff for lint and format at `line-length = 100`; pyright for types.
- Any JavaScript is TypeScript: Vitest for tests, `tsc --noEmit` as the type gate, npm with
  `package-lock.json` committed.

**Serving HTTP**: FastAPI on uvicorn.

**Storing data**: Postgres, run locally by Docker Compose, queried with psycopg 3. A schema that
has to change under real data moves to SQLAlchemy 2 (async) with Alembic migrations. Querying files
without storing records (DuckDB over CSV or Parquet) needs no database.

**A UI**: React and TypeScript on Vite. Zod for shapes
the client checks; Playwright for end-to-end tests. Next.js is for a product that needs server
rendering.

**LLM calls**: the Anthropic Python SDK, called from the backend. Model ids and API usage come
from the `claude-api` skill where the runtime has it, else from Anthropic's current API docs.

**Versions**: the latest release at project start that has cleared the 7-day cooldown, written as
exact pins in `package.json`, with both lockfiles committed. Each project resolves its own;
versions are never copied from another repo.

An existing repo's own extras (another type checker or linter, a generated API client, a CSS
framework) stay that repo's choices. Copy the stack above, not another repo's manifests.
