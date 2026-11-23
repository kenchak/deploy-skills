# Sandbox shortcuts vs production correctness

Sandbox is allowed to be cheap and loose. Production must not inherit those
choices silently. Before promoting, walk this table and note every deviation.

| Area | Sandbox (acceptable) | Production (required) |
|---|---|---|
| Instance count | 1 instance, recreate deploys | 2 apps (A live / B next), blue/green switch |
| Downtime on deploy | ~2 min acceptable | 0 — deploy only to the non-live app |
| Image pull path | Public internet + pull secret | VPC private endpoint (paid attach) once budget allows; otherwise document the risk |
| ACR public ACL | `0.0.0.0/1` + `128.0.0.0/1` (world) | CI egress IPs only |
| Security group | 80/443 (+app port) from anywhere | 443 only, **Cloudflare IP ranges** |
| Database | Serverless + auto-pause, basic series | HA edition, backups, PITR reviewed |
| DB account | DBOwner granted for migration speed | DBOwner (still required on PG16), least-privilege app account where feasible |
| Credentials | May reuse dev creds temporarily | Independent prod credentials; separate RAM user + CI secrets; never copied from dev/sandbox |
| Seed data | Copied from dev | Independent seeds; no dev credentials, no test accounts |
| Migrations | May break and be fixed forward | **Backward compatible only** (expand-then-contract) |
| EIP | Single, reused across deploys | 2 (A+B) during switch; release the stopped app's EIP (unbound EIPs still bill) |
| Env vars | Convenience values | Secret values only via secret mounts / platform env; no defaults that leak |
| Cost tags | `project` + `env=sandbox` | `project` + `env=production` on every resource; verify on first bill |
| Health checks | Health endpoint only | Health endpoint + alarms (5xx, CPU, memory) at minimum |
| Rollback | Redeploy previous tag | DNS switch back to A + restart A (kept 1 week) |
| Change window | Anytime | Explicit user go-live instruction |

## Known deviations in the current TMS sandbox (documented, not forgotten)

- Sandbox DB credentials are the same as dev credentials (tables were copied). Before any external delivery, create an independent test account.
- Image pulls over the public internet (VPC attach quota not purchased).
- ACR public ACL is world-open.
- SG additionally allows 8080 for direct testing.
