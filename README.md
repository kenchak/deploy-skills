# deploy-skills

Deployment skills + tooling shared across projects that use the standard
architecture:

```
dev (local)  →  sandbox (Aliyun SAE, power-user testing)  →  production (SAE, blue/green)
```

## Layout

```
skills/aliyun-deploy/          # the skill (loaded automatically by opencode)
  SKILL.md                     # triggers, non-negotiable rules, tooling usage
  references/
    procedure.md               # provisioning order, deploy flow, blue/green, gates
    pitfalls.md                # 21 verified pitfalls (symptom → cause → fix)
    sandbox-vs-prod.md         # what sandbox may cut corners on, what prod must not
    failure-playbook.md        # diagnosis ladder, rollback, escalation
scripts/deploy.py              # config-driven deploy helper (dry-run by default)
templates/deploy.config.yaml   # per-project config template
```

## Install (once, per machine)

```bash
ln -sfn ~/projects/deploy-skills/skills/aliyun-deploy ~/.config/opencode/skills/aliyun-deploy
```

Skills in `~/.config/opencode/skills/` work in **every** project — no copying
into project directories. Updating this repo updates every project at once.

## Use in a project

1. Copy `templates/deploy.config.yaml` to the project root as `deploy.config.yaml`
   and fill in names / health URLs / required env **key names** (never values).
2. Ensure the project has:
   - an `aliyun` CLI profile per environment,
   - `.env` with the credentials the app needs (gitignored),
   - a Dockerfile + entrypoint that runs migrations then starts the app.
3. Run:

```bash
python3 ~/projects/deploy-skills/scripts/deploy.py preflight --config deploy.config.yaml --env sandbox
python3 ~/projects/deploy-skills/scripts/deploy.py deploy    --config deploy.config.yaml --env sandbox --image-tag <tag> --yes
```

## Scope (v1)

- Python + Docker + PostgreSQL + HTTP API projects.
- Worker / cron projects: **not yet supported** — they need a microservice-type
  SAE app (Web apps freeze CPU when idle). See pitfalls #20.
- Blue/green automation is documented but **not scripted** yet (v2, after the
  first real production promotion validates the procedure).

## Rules

- Credentials never enter this repo. Config holds key names; values live in the
  project's `.env` or a RAM profile.
- Every new deployment lesson gets written back into
  `skills/aliyun-deploy/references/pitfalls.md` in the same turn.

## Versioning

`git tag v1.x` per validated milestone. v1 = sandbox-verified, operations
verified live. v2 = after the first production blue/green promotion.
