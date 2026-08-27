# Application container image.
#
# Stub for the scaffolding phase. The multi-stage build (slim Python 3.12 base,
# non-root user, dependency layer caching, uvicorn entrypoint) is implemented in
# the deployment phase. Left here so the repo structure matches the target
# layout in the build spec.

# TODO(deploy-phase): multi-stage build on python:3.12-slim.
