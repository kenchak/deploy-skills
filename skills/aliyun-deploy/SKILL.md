---
name: aliyun-deploy
description: >
  Standard deployment procedure for projects that use the standard architecture
  (local dev -> Aliyun SAE sandbox -> Aliyun SAE blue/green production).
  Use when the user asks to deploy, promote, roll back, or troubleshoot a
  deployment on Alibaba Cloud — triggers include "deploy to sandbox",
  "deploy to production", "Production go-live", "部署", "上線", "回滾",
  "SAE", "create SAE app", "change order failed", "pod crashed after deploy",
  or any Aliyun/Servless App Engine deployment task. v1 scope:
  Python + Docker + PostgreSQL + HTTP API projects.
---

# Aliyun Deploy (standard architecture)

Deployment playbook distilled from a real production-grade setup. It covers the
full path: first-time environment provisioning, repeat deploys, blue/green
promotion, and failure diagnosis.

## Non-negotiable rules (each one caused a real outage or near-miss)

1. **Never hardcode IDs or values seen in console output.** Truncated output is
   not the truth (a 5-char image sha caused a 40-minute outage). Always resolve
   app ids, instance ids, group ids, and image tags from the API at run time.
2. **`--command` is a plain string** (`/entrypoint.sh`). Passing a JSON array
   makes the literal text reach `runc` → `stat ["/entrypoint.sh"]` → container
   never starts, with an empty log.
3. **`--command` and `--command-args` are a pair.** To revert a command
   override, restore both (args back to empty), not just one.
4. **Deploy is recreate-style** (`min-ready=0`, `max-surge=0`) on a
   single-instance + EIP architecture. A failed deploy therefore means the
   service IS down — no old pod to fall back to.
5. **Promotion is gated** (see `references/procedure.md` §0): do not merge to
   main (sandbox auto-deploy) until the user says "Deploy to Sandbox"; do not
   deploy production until they say "Deploy to Production"; do not switch prod
   traffic until they say "Production go-live".
6. **Never print secrets.** Check for the presence of env keys, never their
   values. Credentials live in each project's gitignored `.env` or a RAM
   profile — never in a skill, config, or note.

## Tooling

Use the bundled script instead of hand-writing CLI commands:

```bash
python3 ~/projects/deploy-skills/scripts/deploy.py preflight --config deploy.config.yaml --env sandbox
python3 ~/projects/deploy-skills/scripts/deploy.py status    --config deploy.config.yaml --env sandbox
python3 ~/projects/deploy-skills/scripts/deploy.py deploy    --config deploy.config.yaml --env sandbox --image-tag <tag> --yes
python3 ~/projects/deploy-skills/scripts/deploy.py logs      --config deploy.config.yaml --env sandbox [--previous]
python3 ~/projects/deploy-skills/scripts/deploy.py err       --config deploy.config.yaml --change-order <id>
```

- `deploy` is **dry-run by default**; only `--yes` executes.
- The script refuses malformed tags (`<14-digit time>-<sha7+>`), refuses when
  required env keys would be lost, restores the full live env set, polls the
  change order until `Status=2`, then verifies every health URL.
- Requires `pyyaml` and a working `aliyun` CLI (profile per environment).

Each project supplies exactly one `deploy.config.yaml` (copy from
`templates/deploy.config.yaml`). The config holds names, health URLs, and
required env **key names** — never values.

## References

| File | When to read it |
|---|---|
| `references/procedure.md` | First-time provisioning (VPC→SG→EIP→RDS→SAE ns→ACR ns→CI→DNS), deploy flow, blue/green promotion, gates |
| `references/pitfalls.md` | Any deployment failure, or before touching command/args, images, secrets, ACR, EIP |
| `references/sandbox-vs-prod.md` | Deciding what is acceptable in sandbox vs what must be corrected for production |
| `references/failure-playbook.md` | Deploy failed / pod crashed / health check fails / change order stuck |

## After every deployment

If anything new was learned (new CLI quirk, new failure mode, config change),
**write it back into `references/pitfalls.md` in the same turn** — this skill is a
single source of truth shared by every project. Do not leave knowledge only in a
chat log.
