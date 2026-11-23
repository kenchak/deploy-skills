#!/usr/bin/env python3
"""
Aliyun SAE deploy helper (v1)

Config-driven deployment tool for the standard project architecture:
  dev (local) -> sandbox (SAE) -> production (SAE blue/green)

Design rules (learned the hard way — see references/pitfalls.md):
  1. NEVER hardcode IDs or values read from truncated console output: resolve
     everything from the API at run time.
  2. SAE CLI --command takes a PLAIN STRING (e.g. "/entrypoint.sh").
     Passing a JSON array makes the literal string reach runc -> container dies.
  3. Deploy is "recreate" style (min-ready=0, max-surge=0) unless the config
     overrides it. In recreate mode a failed deploy means the service IS down:
     always verify health after deploying.
  4. Dry-run is the default for `deploy`; pass --yes to actually execute.
  5. Never print secrets: env values are only inspected for presence.

Usage:
  deploy.py preflight --config deploy.config.yaml --env sandbox
  deploy.py status    --config deploy.config.yaml --env sandbox
  deploy.py deploy    --config deploy.config.yaml --env sandbox --image-tag <tag> [--yes]
  deploy.py err       --config deploy.config.yaml --change-order <id>
  deploy.py logs      --config deploy.config.yaml --env sandbox [--previous]
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("需要 pyyaml：pip install pyyaml")

OK = "\033[32mOK\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"


# ---------------------------------------------------------------- utilities

def log(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    log(f"\n=== {msg} ===")


def sh(args: list[str], timeout: int = 90) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    except FileNotFoundError:
        return "__NOTFOUND__ aliyun"


def jloads(text: str):
    """Extract the first JSON object/array from mixed CLI output."""
    for opener in ("{", "["):
        i = text.find(opener)
        if i >= 0:
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                continue
    return None


def find_key(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and v not in (None, ""):
                return v
            r = find_key(v, key)
            if r not in (None, ""):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r not in (None, ""):
                return r
    return None


class Ctx:
    def __init__(self, cfg: dict, env: str):
        if env not in (cfg.get("environments") or {}):
            sys.exit(f"config 沒有 environments.{env}")
        self.cfg = cfg
        self.env_cfg = cfg["environments"][env]
        self.env = env
        self.region = self.env_cfg["region"]
        self.profile = self.env_cfg["profile"]
        self.namespace = self.env_cfg["namespace"]
        self.app_name = self.env_cfg["app_name"]

    def cli(self, *args: str, timeout: int = 90) -> str:
        return sh(["aliyun", *args, "--region", self.region,
                   "--profile", self.profile], timeout=timeout)

    # -- resolution (never hardcode IDs) -----------------------------------

    def list_apps(self) -> list[dict]:
        d = jloads(self.cli("sae", "ListApplications")) or {}
        return (d.get("Data") or {}).get("Applications") or []

    def resolve_app(self) -> dict:
        for a in self.list_apps():
            if a.get("NamespaceId") == self.namespace and a.get("AppName") == self.app_name:
                return a
        sys.exit(f"找不到應用：namespace={self.namespace} app={self.app_name}")

    def resolve_group_id(self, app_id: str) -> str:
        d = jloads(self.cli("sae", "describe-application-groups", "--app-id", app_id)) or {}
        groups = d.get("Data") or []
        # prefer default group; else first
        for g in groups:
            if g.get("GroupName") == "_DEFAULT_GROUP":
                return g.get("GroupId")
        return groups[0].get("GroupId") if groups else ""

    def instances(self, app_id: str, group_id: str) -> list[dict]:
        d = jloads(self.cli("sae", "describe-application-instances",
                            "--app-id", app_id, "--group-id", group_id)) or {}
        return (d.get("Data") or {}).get("Instances") or []

    def app_config(self) -> dict:
        d = jloads(self.cli("sae", "describe-application-config",
                            "--app-id", self.resolve_app().get("AppId"))) or {}
        return d.get("Data") or {}

    def change_orders(self, app_id: str) -> list[dict]:
        d = jloads(self.cli("sae", "list-change-orders", "--app-id", app_id)) or {}
        return (d.get("Data") or {}).get("ChangeOrderList") or []


def health_check(url: str, insecure: bool = False, timeout: int = 8):
    ctxssl = ssl.create_default_context()
    if insecure:
        ctxssl.check_hostname = False
        ctxssl.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "deploy-skills/1"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctxssl) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # noqa: BLE001
        return f"ERR({type(e).__name__})"


# ---------------------------------------------------------------- commands

def cmd_preflight(ctx: Ctx) -> int:
    problems: list[str] = []
    warnings: list[str] = []

    step("1) aliyun CLI 與 profile")
    ver = sh(["aliyun", "version"])
    if ver.startswith("__"):
        problems.append("aliyun CLI 不可用")
    else:
        log(f"  CLI: {ver.splitlines()[0] if ver else '?'}")
    log(f"  profile: {ctx.profile}  region: {ctx.region}")

    step("2) 解析應用")
    app = ctx.resolve_app()
    app_id = app.get("AppId")
    log(f"  AppId: {app_id}")
    log(f"  Image: {app.get('ImageUrl')}")
    log(f"  Instances: {app.get('Instances')} (running {app.get('RunningInstances')})")

    step("3) 環境變數完整性（只驗存在，不印值）")
    cfg = ctx.app_config()
    envs = cfg.get("Envs")
    if isinstance(envs, str):
        try:
            envs = json.loads(envs)
        except json.JSONDecodeError:
            envs = []
    names = {e.get("name") for e in (envs or [])}
    required = (ctx.cfg.get("deploy") or {}).get("required_env_keys") or []
    missing = [k for k in required if k not in names]
    if missing:
        problems.append(f"缺少必要環境變數：{missing}")
        log(f"  {FAIL} 缺 {missing}")
    else:
        log(f"  {OK} 必要鍵齊全（共 {len(names)} 個 env）")

    step("4) 容器設定健全性")
    command = cfg.get("Command")
    args = cfg.get("CommandArgs")
    log(f"  Command:    {command!r}")
    log(f"  CommandArgs:{args!r}")
    if isinstance(command, str) and command.strip().startswith("["):
        problems.append('Command 是 JSON 字串（會字面量直通 runc）→ 必須改成純字串，如 "/entrypoint.sh"')
    expected = (ctx.cfg.get("container") or {}).get("command")
    if expected and command != expected:
        warnings.append(f"線上 Command={command!r} 與 config 期望 {expected!r} 不同")

    step("5) 實例狀態")
    gid = ctx.resolve_group_id(app_id)
    inst = ctx.instances(app_id, gid)
    if not inst:
        warnings.append("目前無實例（已停止或從未啟動）")
    for a in inst:
        log(f"  {a.get('InstanceId')} | {a.get('InstanceContainerStatus')} | "
            f"restarts={a.get('InstanceContainerRestarts')} | "
            f"{(a.get('ImageUrl') or '').split(':')[-1]}")

    step("6) 健康端點現況（資訊性）")
    for h in (ctx.env_cfg.get("health") or []):
        code = health_check(h["url"], h.get("insecure", False))
        log(f"  {h['url']} -> {code}")

    step("7) 最近 change order")
    for c in ctx.change_orders(app_id)[:3]:
        log(f"  status={c.get('Status')} | {c.get('Description')} | {c.get('CreateTime')}")

    step("結果")
    for w in warnings:
        log(f"  {WARN} {w}")
    for p in problems:
        log(f"  {FAIL} {p}")
    if problems:
        return 1
    log(f"  {OK} preflight 通過")
    return 0


def cmd_status(ctx: Ctx) -> int:
    app = ctx.resolve_app()
    app_id = app.get("AppId")
    step(f"應用 {ctx.app_name} @ {ctx.namespace}")
    for k in ("AppId", "ImageUrl", "Instances", "RunningInstances", "Cpu", "Mem", "EnableIdle"):
        log(f"  {k}: {app.get(k)}")
    gid = ctx.resolve_group_id(app_id)
    step("實例")
    for a in ctx.instances(app_id, gid):
        log(f"  {a.get('InstanceId')} | {a.get('InstanceContainerStatus')} | "
            f"restarts={a.get('InstanceContainerRestarts')} | img={(a.get('ImageUrl') or '').split(':')[-1]}")
    step("健康")
    for h in (ctx.env_cfg.get("health") or []):
        log(f"  {h['url']} -> {health_check(h['url'], h.get('insecure', False))}")
    step("最近 3 個 change order")
    for c in ctx.change_orders(app_id)[:3]:
        log(f"  status={c.get('Status')} | {c.get('Description')} | {c.get('CreateOrderId') or c.get('ChangeOrderId')}")
    return 0


def dump_pipeline(ctx: Ctx, co_id: str) -> None:
    d = jloads(ctx.cli("sae", "describe-change-order", "--change-order-id", co_id)) or {}
    pipe = find_key(d, "CurrentPipelineId")
    if not pipe:
        log("  (無 pipeline 資訊)")
        return
    p = jloads(ctx.cli("sae", "describe-pipeline", "--pipeline-id", pipe)) or {}
    for stage in (p.get("Data") or {}).get("StageList", []) or []:
        for task in stage.get("TaskList", []) or []:
            mark = "OK" if task.get("Status") == 2 else f"FAIL({task.get('Status')})"
            log(f"  [{mark}] {task.get('TaskName')}")
            if task.get("Status") != 2:
                import html as _html
                msg = _html.unescape(str(task.get("Message", ""))).replace("<br/>", "\n")
                for line in msg.splitlines()[:40]:
                    log(f"      {line}")


def poll_change_order(ctx: Ctx, app_id: str, co_id: str, cap: int = 90) -> int | None:
    for i in range(cap):
        st = None
        for c in ctx.change_orders(app_id):
            if c.get("ChangeOrderId") == co_id:
                st = c.get("Status")
                break
        if i % 4 == 0 or st in (2, 3, 10):
            log(f"  co poll {i}: {st}")
        if st in (2, 3, 10, 6, 8, 9, 11):
            return st
        time.sleep(10)
    return None


def cmd_deploy(ctx: Ctx, image_tag: str, yes: bool) -> int:
    deploy_cfg = ctx.cfg.get("deploy") or {}
    acr_cfg = ctx.cfg.get("acr") or {}
    container = ctx.cfg.get("container") or {}

    # --- guard: tag format (the 2026-11-10 truncated-tag incident) ---
    if not re.fullmatch(r"\d{14}-[0-9a-f]{7,}", image_tag):
        log(f"{FAIL} tag `{image_tag}` 不符合 `<14位時間>-<≥7位sha>` 格式。")
        log("  2026-11-10 事故：人眼看到的截斷 tag 被硬編碼 → 拉映像 404 → 服務中斷。")
        log("  請從 CI run 或 ACR tags/list 取完整 tag 再重試。")
        return 1

    app = ctx.resolve_app()
    app_id = app.get("AppId")
    acr_ns = ctx.env_cfg.get("acr_namespace")
    if not acr_ns:
        log(f"{FAIL} config 缺 environments.{ctx.env}.acr_namespace")
        return 1
    image_repo = ctx.cfg.get("image_repo")
    if not image_repo:
        log(f"{FAIL} config 缺 image_repo")
        return 1
    repo = f"{acr_ns}/{image_repo}"
    registry = acr_cfg.get("registry", "tms-acr-registry.cn-hongkong.cr.aliyuncs.com")
    image_url = f"{registry}/{repo}:{image_tag}"

    step("1) 讀取現行環境變數（保留全部，避免部署時遺失）")
    cfg = ctx.app_config()
    envs = cfg.get("Envs")
    if isinstance(envs, str):
        envs = json.loads(envs or "[]")
    envs = [{"name": e["name"], "value": e["value"]} for e in (envs or [])]
    names = {e["name"] for e in envs}
    required = deploy_cfg.get("required_env_keys") or []
    missing = [k for k in required if k not in names]
    if missing:
        log(f"  {FAIL} 缺少必要環境變數 {missing} —— 中止（部署會抹掉現有 env）")
        return 1
    log(f"  {OK} {len(envs)} 個 env 保留（{', '.join(sorted(names))}）")

    step("2) 組裝 deploy 命令")
    mounts = container.get("secret_mounts") or deploy_cfg.get("secret_mounts") or []
    args_list = [
        "aliyun", "sae", "deploy-application",
        "--region", ctx.region, "--profile", ctx.profile,
        "--app-id", app_id,
        "--image-url", image_url,
        "--acr-instance-id", acr_cfg.get("instance_id", ""),
        "--custom-image-network-type", acr_cfg.get("network_type", "internet"),
        "--min-ready-instances", str(deploy_cfg.get("min_ready_instances", 0)),
        "--max-surge-instances", str(deploy_cfg.get("max_surge_instances", 0)),
        "--envs", json.dumps(envs),
        "--change-order-desc", f"{ctx.cfg.get('project', 'app')} {ctx.env} deploy {image_tag}",
    ]
    pull_secrets = acr_cfg.get("pull_secret_ids") or []
    if pull_secrets:
        args_list += ["--image-pull-secrets", ",".join(str(s) for s in pull_secrets)]
    if mounts:
        args_list += ["--secret-mount-desc", json.dumps(mounts)]
    if container.get("command"):
        # plain string ONLY (never JSON array)
        args_list += ["--command", str(container["command"])]
    if container.get("args") is not None:
        args_list += ["--command-args", json.dumps(container["args"])]

    log(f"  image: {image_url}")
    log(f"  command: {container.get('command')!r}  args: {container.get('args')!r}")
    log(f"  recreate: min-ready={deploy_cfg.get('min_ready_instances', 0)} "
        f"max-surge={deploy_cfg.get('max_surge_instances', 0)}")

    if not yes:
        log(f"\n{WARN} dry-run：未執行。確認無誤後加 --yes 真正部署。")
        return 0

    step("3) 部署（recreate：舊實例會被先移除）")
    out = sh(args_list, timeout=150)
    d = jloads(out) or {}
    co_id = find_key(d, "ChangeOrderId")
    if not co_id:
        log(f"  {FAIL} 未取得 ChangeOrderId：{out[:500]}")
        return 1
    log(f"  ChangeOrderId: {co_id}")

    step("4) 等待 change order（Status=2 才算成功）")
    st = poll_change_order(ctx, app_id, co_id)
    log(f"  status={st}")
    if st != 2:
        log(f"  {FAIL} 部署失敗 —— 診斷資訊：")
        dump_pipeline(ctx, co_id)
        return 2

    step("5) 部署後健康驗證")
    gid = ctx.resolve_group_id(app_id)
    inst = ctx.instances(app_id, gid)
    for a in inst:
        log(f"  {a.get('InstanceId')} | {a.get('InstanceContainerStatus')} | "
            f"img={(a.get('ImageUrl') or '').split(':')[-1]}")

    ok = False
    for attempt in range(1, 11):
        results = [(h["url"], health_check(h["url"], h.get("insecure", False)))
                   for h in (ctx.env_cfg.get("health") or [])]
        log(f"  嘗試 {attempt}: " + ", ".join(f"{u} -> {c}" for u, c in results))
        ok = all(str(c) == "200" for _, c in results) if results else True
        if ok:
            break
        time.sleep(10)
    if not ok:
        log(f"  {FAIL} 健康驗證未通過 —— 可能進入 CrashLoop。檢查順序：")
        log("    1) Console Pod Events（OCI 原文最精準）")
        log("    2) deploy.py logs --env <env> --previous")
        log("    3) deploy.py err --change-order <id>")
        return 3

    log(f"\n{OK} 部署完成：{ctx.env} ← {image_tag}")
    return 0


def cmd_err(ctx: Ctx, co_id: str) -> int:
    step(f"change order {co_id} 的 pipeline")
    dump_pipeline(ctx, co_id)
    return 0


def cmd_logs(ctx: Ctx, previous: bool) -> int:
    app = ctx.resolve_app()
    gid = ctx.resolve_group_id(app.get("AppId"))
    inst = ctx.instances(app.get("AppId"), gid)
    if not inst:
        log(f"{FAIL} 無實例可讀日誌")
        return 1
    iid = inst[0].get("InstanceId")
    step(f"實例 {iid} 日誌{'（previous）' if previous else ''}")
    args = ["aliyun", "sae", "describe-instance-log", "--instance-id", iid]
    if previous:
        args += ["--previous", "true"]
    out = ctx.cli(*args[2:])  # keep region/profile
    d = jloads(out)
    text = (d or {}).get("Data") if isinstance(d, dict) else None
    log((text or "(空日誌)")[:4000])
    if not text and not previous:
        log(f"\n{WARN} 空日誌常見於容器未能啟動：改用 --previous，或看 Console Pod Events。")
    return 0


# ---------------------------------------------------------------- main

def load_cfg(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"找不到 config：{path}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Aliyun SAE deploy helper (config-driven)")
    ap.add_argument("command", choices=["preflight", "status", "deploy", "err", "logs"])
    ap.add_argument("--config", default="deploy.config.yaml")
    ap.add_argument("--env", default="sandbox", help="sandbox | production")
    ap.add_argument("--image-tag", help="完整映像 tag（<14位時間>-<sha7+>）")
    ap.add_argument("--change-order", help="change order id（err 用）")
    ap.add_argument("--previous", action="store_true", help="讀上一個容器的日誌")
    ap.add_argument("--yes", action="store_true", help="實際執行部署（預設 dry-run）")
    a = ap.parse_args()

    cfg = load_cfg(a.config)

    if a.command == "err":
        if not a.change_order:
            sys.exit("err 需要 --change-order")
        ctx = Ctx(cfg, a.env)
        return cmd_err(ctx, a.change_order)

    ctx = Ctx(cfg, a.env)
    if a.command == "preflight":
        return cmd_preflight(ctx)
    if a.command == "status":
        return cmd_status(ctx)
    if a.command == "logs":
        return cmd_logs(ctx, a.previous)
    if a.command == "deploy":
        if not a.image_tag:
            sys.exit("deploy 需要 --image-tag")
        return cmd_deploy(ctx, a.image_tag, a.yes)
    return 1


if __name__ == "__main__":
    sys.exit(main())
