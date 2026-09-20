# Agent dependencies

The Dependencies settings page prepares additions to `local.json`. This installation-local recipe
is deliberately ignored by Git. Review its patch before accepting: package managers may install
transitive dependencies and execute publisher-provided installation scripts. Unpinned packages are
resolved at installation time.

Docker copies this directory into the image build. The rebuilder service must be running and updated
to support dependency jobs. Python packages live in `/opt/agent-python`, outside the application
virtual environment and its persistent volume. Native installations use a separate versioned Python
environment. System package installation never invokes sudo or another privilege-elevation tool.

The first rollout needs a rebuilt application image and a recreated rebuilder service, not just an
application restart. The rebuilder must have access to the same compose project and build context:

```sh
docker compose -f deploy/compose.yaml --env-file .env --profile selfdev up -d --build daedalus rebuilder
```

On native systems, Python installation requires uv. Automatic system installation supports apt when
already running with sufficient privileges, and Homebrew formulae on macOS. Other platforms and
unprivileged apt installations show system installation as unavailable; install those packages with
the operating system's own tools. The application never attempts to acquire administrator rights.
