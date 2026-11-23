# Procedure

## §0 Release gates (never bypass)

| User says | Meaning for the agent |
|---|---|
| (nothing) | Work on a branch; test locally only. **Do not merge to main** (main auto-deploys sandbox). |
| "Deploy to Sandbox" | Merge to main → CI builds image → deploys sandbox. Verify health after. |
| "Deploy to Production" | Deploy the image to the **non-live** prod app (B). No traffic switch yet. |
| "Production go-live" | Switch prod traffic A→B (DNS record edit). Old app keeps running for rollback. |
| (silence after go-live) | Keep the old app stopped-but-intact for 1 week as rollback window. |

## §1 First-time provisioning (one env)

Order matters; verify each step before the next.

1. **RAM user + profile** — one user per environment (`<project>-sb`, `<project>-pd`), never shared. Store AK in gitignored `.env` + `~/.aliyun/config.json` profile. Two-phase policy: open permissions first, tighten to tag-scoped later.
2. **Pre-create cost tags** — `project:<name>` + `env:sandbox|production`. Enabling them for cost allocation is Console-only and takes T+1.
3. **VPC + vSwitch** — independent per project (never share with another project's VPC).
4. **Security group** — sandbox: 80/443 (and app port if needed) → tighten later. **Production: restrict 443 to Cloudflare IP ranges**, not 0.0.0.0/0.
5. **EIP** — pay-by-traffic. One EIP per running instance. **Check account EIP quota first**: every prod blue/green cycle needs 2 (A+B), and quota is account-wide.
6. **RDS PostgreSQL** — sandbox: Serverless/basic + auto-pause (cheap, accepts wake latency). Production: HA edition. Grant `DBOwner` to the app account (PG16 public schema CREATE requires it). Whitelist the VPC CIDR only.
7. **SAE namespace** — bind the VPC. Naming `{project}-{sb|pd}`.
8. **ACR namespace + repo** — reuse the existing ACR instance if possible. Enable the public endpoint, and set the public ACL (`0.0.0.0/1` + `128.0.0.0/1`; `/0` is rejected). Tighten ACL to CI egress IPs for production.
9. **SAE secrets** — two kinds:
   - `dockerconfigjson` pull secret. `create-secret --secret-data` takes the **inner** JSON: `{"SecretData": "{\".dockerconfigjson\":\"<b64>\"}"}` — opposite of what the CLI docs imply.
   - TLS origin cert (`kubernetes.io/tls`), mounted via `secret-mount-desc` with key `sae-sys-secret-all`.
10. **SAE application** — image type, single instance, EIP mode. Then verify: `deploy.py preflight` must pass.
11. **CI workflow** — build → push to ACR (`<ns>/<repo>:<ts-sha>`) → `sae deploy-application` with full parameters → **poll change order until `Status=2`**, fail otherwise. Recreate params are mandatory (see pitfalls).
12. **DNS + TLS** — Cloudflare proxied (orange cloud), SSL mode **Full (strict)**, origin cert on the container with `PORT=443` + `SSL_CERTFILE`/`SSL_KEYFILE`. `http→https` always on. **Never Flexible** (would expose secrets/tokens in plaintext).
13. **Verification matrix** — origin health (insecure ok), edge health (must pass cert validation), HTTP→HTTPS 301, unauthenticated API call returns 401 (auth works), container log shows successful migration.

## §2 Repeat deploy (normal path)

```bash
# 0) gates: only after the user's instruction
deploy.py preflight --config deploy.config.yaml --env sandbox
deploy.py deploy    --config deploy.config.yaml --env sandbox --image-tag <full tag>
deploy.py deploy    --config deploy.config.yaml --env sandbox --image-tag <full tag> --yes
```

Sandbox downtime (~2 min, recreate) is acceptable. Production must go through blue/green (§3).

## §3 Blue/green production (standard flow, validated in production rollout)

1. Prod namespace holds **two apps**: A (live) and B (idle/stale). Each has its own EIP.
2. "Deploy to Production" → deploy new image to **B** (recreate within B; A untouched, zero customer impact).
3. Review URL (e.g. `api-review.<domain>`) points at B for the final review.
4. "Production go-live" → edit the **live DNS record** (`api.<domain>`) from A's EIP to B's EIP. Cloudflare edge picks this up in seconds; rollback = edit back.
5. After go-live: stop A (compute billing stops), **release A's EIP** (an unbound pay-as-you-go EIP still bills a retention fee). Keep A's config for 1 week as the rollback path (restart + redeploy old image ≈ 5 min).
6. Next cycle: wake A, deploy next version to A, review, switch DNS back. A/B rotate.

**Blueprint rule:** while both apps share one production database, every migration must be **backward compatible** (expand-then-contract: add nullable columns/tables, never drop/rename while the old version may still serve). Violating this breaks live A the moment B boots, because the container runs `alembic upgrade head` at startup.

**Review caveat:** during review, B writes to the real production database. Test records are real records.

## §4 Pre-production checklist

- [ ] EIP quota allows +1 (blue/green needs 2 simultaneous).
- [ ] Prod DB edition = HA, DBOwner granted, backup policy set.
- [ ] SG restricted (Cloudflare IPs), ACR ACL tightened to CI egress.
- [ ] Separate RAM user + CI secrets for prod; never reuse sandbox credentials.
- [ ] Prod seed data uses **independent** credentials (never copy dev creds).
- [ ] Cost tags `project`/`env=production` on every resource.
- [ ] Migration review: all pending migrations are expand-then-contract safe.
