# Working in this repo

Preflight, the idea-capture plugin for the Hermes Dashboard, live on the Ori8 Hermes dashboard.
`README.md` explains how to install it and documents its storage, API and safety posture.

- `network: false` under `permissions` in `plugin.yaml` is a deliberate security boundary. Turning it
  on, as the v1.1 work in PR #3 does, is Mike's decision, not a session's.
- Mission Control Dashboard (`Ori8-Automations/missioncontroldashboard`) embeds a port of this plugin,
  so a change here may need porting there too.
- Run the checks with `./tests/run_tests.sh`. It needs a Python with `fastapi` and `httpx`; point
  `PYTHON` at a venv that has them, e.g. `PYTHON=/path/to/venv/bin/python ./tests/run_tests.sh`.
- This repo is public. Never commit client names, internal hostnames or IP addresses, credentials,
  or exports of a real Preflight data root.
- Start from the default branch, `main`. When the work is done, open a pull request into `main` and
  tell Mike it is ready to merge. Finished work should never be left on a branch without a pull request.
