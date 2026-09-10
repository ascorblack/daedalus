# Security

Daedalus runs an agent with unrestricted tools inside its own container. The container is the boundary;
everything below describes what stands between the agent and the rest of your machine, and how to report
a hole in it.

## What protects the operator

- **Keys never enter the agent container.** Provider keys live in `../daedalus-secrets/keyproxy.env`;
  the key proxy injects them, meters every call, and refuses calls above the daily cap.
- **Self-change is reviewed.** The agent edits its own code in a git worktree and opens a pull request;
  nothing reaches `main` without the operator. The supervisor (outside the agent's reach) runs the
  preflight and rolls back a build that fails it.
- **A policy sits in front of every tool call** (`daedalus/host/policy.py`): commands that act on the
  machine, the operator's paths or the operator's checkouts are refused; a forced push, deleting a
  workspace or a host outside the egress allowlist wait for the operator's approval key. The built-in
  rules change only through a pull request; the operator's rules can tighten them, never loosen them.
- **Secrets are masked** in tool output, logs, transcripts, proposal cards and pull requests.
- **The sandbox fails closed.** With `tools.exec.sandbox = "workspace"`, a container that cannot create
  namespaces refuses commands instead of running them bare.

## What it does not protect against

- A model that is determined to misuse the container's own resources (CPU, disk, the agent's GitHub
  organisation) can do so within the policy's limits; the policy catches the accidental and the lazy case.
- Anything you mount into the container is the agent's. Mount only what it should have.
- Telegram is the operator channel: whoever controls the operator's Telegram account controls the agent.

## Reporting

Open a private security advisory on the GitHub repository, or write to the address on the maintainer's
profile. Include the version (the commit on `main`), what you did, what happened, and what you expected.
Please do not open a public issue for a vulnerability before it is fixed.
