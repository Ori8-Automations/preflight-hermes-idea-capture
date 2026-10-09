# Working in this repo

Preflight, the idea-capture plugin for the Hermes Dashboard, live on the Ori8 Hermes dashboard.
`README.md` explains how to install it and documents its storage, API and safety posture.

- Since v1.1, `plugin.yaml` declares `network: true`, used only by the optional context review:
  webhook delivery to a configured reviewer and bounded public-source retrieval. Both stay inert until
  an operator enables `context_review` and configures a route. Keep it that way: nothing reaches out
  by default, and the SSRF guards and injected HTTP seams stay. Wider network use is Mike's call.
- Mission Control Dashboard (`Ori8-Automations/missioncontroldashboard`) embeds a port of Preflight
  1.0.8, from before v1.1, so a change here may need porting there too.
- Run the checks with `./tests/run_tests.sh`. It needs a Python with `fastapi` and `httpx`; point
  `PYTHON` at a venv that has them, e.g. `PYTHON=/path/to/venv/bin/python ./tests/run_tests.sh`.
- This repo is public. Never commit client names, internal hostnames or IP addresses, credentials,
  or exports of a real Preflight data root.
- Start from the default branch, `main`. When the work is done, open a pull request into `main` and
  tell Mike it is ready to merge. Finished work should never be left on a branch without a pull request.
