# Pitfalls (each verified in a real deployment, Aliyun CLI 3.5.3, cn-hongkong)

Format: **symptom → cause → fix**

## SAE deploy

1. **Container never starts, empty log, `RunContainerError` / `BackOff`**
   → `--command` was given a JSON array (`["/entrypoint.sh"]`). The literal string reaches `runc`:
   `exec: "[\"/entrypoint.sh\"]": stat: no such file or directory`.
   → Pass a plain string: `--command /entrypoint.sh`. Never JSON.

2. **Deploy succeeded earlier, now image pull 404 `not found`**
   → A truncated value copied from console/CLI display was hardcoded (5-char sha vs 7-char).
   → Always resolve full values from the API (`ListApplications`, ACR `tags/list`). `deploy.py` rejects tags not matching `<14 digits>-<sha7+>`.

3. **Restore deploy still fails after fixing `--command`**
   → `CommandArgs` still holds the old script; `Command` and `CommandArgs` must be set as a pair.
   → Restore both (args back to empty / omit).

4. **`EipNotEnough` during deploy, then `ImagePullBackOff`**
   → Surge deployment allocates a new pod that needs its own EIP while the account has none idle.
   → Always deploy recreate-style: `--min-ready-instances 0 --max-surge-instances 0`. Consequence: a failed recreate deploy = real downtime (no old pod).

5. **`Application.ChangerOrderRunning`**
   → A previous change order is still running.
   → Poll `list-change-orders` until terminal (2/3) before submitting a new deploy.

6. **Pipeline says only "Task timeout", no cause**
   → The pipeline summary is lossy. The precise OCI/runtime message is in **Console → app → Pod Events**.
   → Read Pod Events first (they show the raw `runc`/kubelet error).

7. **`describe-instance-log` returns empty**
   → Container died before producing logs (or the pod log file was never created); `--previous` may also be empty in that case.
   → Check Pod Events; also verify the container command and that the image actually exists.

8. **`list-app-events` errors out**
   → Missing pagination flag.
   → Pass `--current-page 1`.

## Image / ACR

9. **Container dies with `alembic: not found` (or any optional-dependency module missing)**
   → Dockerfile did a bare `pip install .` while the code needs an extra.
   → Install the required extra explicitly, e.g. `pip install ".[upstream]"`.

10. **Private image pull fails though credentials are correct**
    → ACR public endpoint disabled by default.
    → `aliyun cr update-instance-endpoint-status --instance-id <id> --enable true`.

11. **Pull fails from a fresh VPC**
    → SAE pulling over `-vpc` domain requires the VPC-attach quota (paid add-on). Without it: `INSTANCE_ACCESS_VPC_LIMIT_EXCEED`.
    → Use `--custom-image-network-type internet` + a pull secret.

12. **ACR public ACL set to `0.0.0.0/0` is rejected**
    → Use `0.0.0.0/1` + `128.0.0.0/1`. Leaving the ACL empty = deny all.

13. **`/v2/` returns 401 even with correct basic auth**
    → Registry answers with a Bearer challenge; preemptive basic auth always gets 401 (false negative). Obtain a token from the auth realm, then call `tags/list` with the Bearer token. Token fields: `TempUsername` / `AuthorizationToken`.

## Secrets

14. **`create-secret --secret-data` "succeeds" but the secret is wrong**
    → The CLI wants the **inner** payload: `{"SecretData": "<inner JSON string>"}` — same for `dockerconfigjson` and `kubernetes.io/tls`. Documented shape differs.

## Network / EIP / DB

15. **SG blocks the app port**
    → Only 80/443 were opened. Add the app port (`ecs AuthorizeSecurityGroup` — the SG API lives in the **ECS** product line, not vpc).

16. **Unbound EIP keeps charging**
    → Pay-as-you-go EIPs bill a retention fee while unbound (official). Release them when stopping an environment if the IP is not needed.

17. **Local machine cannot reach the RDS private domain**
    → The dev box is not inside the project VPC — it is a timeout black hole, not a credentials problem. Run DB operations via API or inside a container in the VPC.

18. **RDS shows `STOPPED` unexpectedly**
    → Serverless auto-pause. The first connection wakes it; retry rather than treating it as an outage.

19. **PG migration fails with "permission denied for schema public"**
    → PG16: CREATE on `public` is owner-only. Grant the app account `DBOwner` (`rds GrantAccountPrivilege --AccountPrivilege DBOwner`).

## Runtime behaviour

20. **Background threads/schedulers never run in a Web-type SAE app**
    → Web apps only get CPU while a request is being processed — idle instances freeze CPU, so background loops starve. The "always allocate fixed CPU" Web option was retired (2024-07).
    → Worker/cron projects must be deployed as **microservice-type** SAE apps. For Web apps, drive scheduled work from an external timer (HTTP trigger).

21. **Migrations break the still-running old version during blue/green**
    → The container runs migrations at startup; both apps share one DB.
    → Enforce expand-then-contract migrations (see `procedure.md` §3).
