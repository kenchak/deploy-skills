# Failure playbook

Work top-down; stop at the first step that explains the failure.

## 1. The deploy command itself was rejected

- `InvalidParameter` on `--command-args` → format wrong (e.g. empty string). Omit it or pass a JSON array.
- `InvalidParameter` on `--command` → you passed a JSON array. Use a plain string.
- `Application.ChangerOrderRunning` → wait for the running change order to finish, then resubmit.
- `NoPermission` / `Forbidden` → wrong CLI profile (sandbox credentials hitting prod or vice versa).

## 2. Change order ended with Status=3

```bash
deploy.py err --config deploy.config.yaml --change-order <id>
```

Then read the failing task message, and in parallel:

- **Console → app → Pod Events** — this is where the raw `runc`/kubelet message lives.

Classify what you see:

| Message contains | Go to |
|---|---|
| `not found` while pulling image | §3 image problems |
| `stat [...]` / `exec:` | §4 command/args problems |
| `BackOff` / `CrashLoopBackOff` | §5 container crash |
| `EipNotEnough` | surge deploy was used — switch to recreate params |
| `INSTANCE_ACCESS_VPC_LIMIT_EXCEED` | VPC pull path without the paid attach — use internet pull |

## 3. Image problems

- Verify the tag exists: `curl <registry>/v2/<repo>/tags/list` via Bearer token (basic auth always 401 — false negative).
- Confirm the tag is the **full** one (7+ char sha). Truncated tags are the classic 404.
- If pull credentials are suspect, re-check the pull secret (`dockerconfigjson` inner-JSON quirk).

## 4. Command/args problems

- `exec: "[\"/entrypoint.sh\"]": stat: no such file` → JSON array passed to `--command`.
- Empty log + immediate restart → the container entrypoint exited (bad command path, missing interpreter).
- Fix `--command` and `--command-args` **together**.

## 5. Container crash

```bash
deploy.py logs --config deploy.config.yaml --env <env>            # current
deploy.py logs --config deploy.config.yaml --env <env> --previous  # crashed container
```

Common causes:

- migration failure at startup (DB unreachable / not owner / bad migration) — `alembic upgrade head` runs in the entrypoint;
- missing required env (JWT secret, DATABASE_URL);
- missing mounted file (TLS cert path mismatch);
- module missing in the image (a needed extra was not installed).

Note: an empty log usually means the process never started — prefer Pod Events over logs in that case.

## 6. Deploy OK but health check fails

1. Instance status: `Running`? restarts rising?
2. Origin health (`https://<eip>/health`, insecure) vs edge health.
3. Origin 200 + edge failing → TLS/DNS layer: Cloudflare SSL mode must be Full (strict); origin cert must cover the hostname and be mounted; `PORT=443` + `SSL_CERTFILE`/`SSL_KEYFILE` set.
4. Nothing answers → SG missing the port, or the app bound to the wrong port.
5. DB-related 500s → serverless DB may have auto-paused; retry; check connection string host (private domain needs VPC).

## 7. Rollback

- **Before go-live (prod B):** do nothing — A is still live. Fix B or leave it.
- **After go-live:** switch the DNS record back to A's EIP (Cloudflare, seconds), then confirm A's health. Keep B for diagnosis.
- **Sandbox:** redeploy the previous tag (`deploy.py deploy --image-tag <previous>`).

## 8. Escalate to the user (human-only actions)

- Console-only settings (cost-tag activation, some SAE UI toggles).
- Quota increases (EIP, VPC attach), billing issues.
- DNS/Cloudflare changes if no API token is configured.
