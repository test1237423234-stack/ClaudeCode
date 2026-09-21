#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from rename import transform  # noqa: E402

API = "https://api.github.com"
UA = "skidsaken-mirror/2.0"

DEFAULTS = {
    "source_url": "https://raw.githubusercontent.com/jeevacation780/"
                  "repository-for-kings/refs/heads/main/cat.lua",
    "source_repo": "jeevacation780/repository-for-kings",
    "source_path": "cat.lua",
    "src_name": "catsaken",
    "dst_name": "skidsaken",
    "target_repo": "",
    "target_url": "",
    "target_branch": "main",
    "target_file": "cat.lua",
    "workdir": ".mirror-work",
    "state": ".mirror-state.json",
    "interval": 600,
    "attribution": True,
    "telemetry_url": "",
    "patches": [],
    "assets": [],
    "rewrite_url": [],
    "git_name": "skidsaken-bot",
    "git_email": "skidsaken-bot@users.noreply.github.com",
    "local_dir": "",
}
# ключи, которые можно задать в конфиге и переопределить флагами командной строки
CONFIG_KEYS = [
    "source_url", "source_repo", "source_path", "src_name", "dst_name",
    "target_repo", "target_url", "target_branch", "target_file", "workdir",
    "state", "interval", "attribution", "telemetry_url", "patches", "assets",
    "rewrite_url", "git_name", "git_email", "local_dir",
]


# --------------------------------------------------------------------------- #
# HTTP / git helpers
# --------------------------------------------------------------------------- #
def http_get(url: str, token: str | None = None, binary: bool = False, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if not binary:
        req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", errors="replace")


def log(msg: str = "") -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def run(cmd: list[str], cwd: str | None = None, check: bool = True, quiet: bool = False):
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0 and check:
        raise RuntimeError(f"`{' '.join(cmd[:3])} …` -> код {res.returncode}\n{res.stderr.strip()}")
    if res.stdout.strip() and not quiet:
        print("   " + res.stdout.strip().replace("\n", "\n   "))
    return res


def auth_url(target_url: str, token: str | None) -> str:
    """https://github.com/u/r.git + токен -> URL с авторизацией (на диск не пишется)."""
    if not token or "github.com" not in target_url:
        return target_url
    if "@" in target_url.split("//", 1)[-1]:
        return target_url
    return re.sub(r"^https://", f"https://x-access-token:{token}@", target_url)


def ensure_worktree(workdir: str, branch: str, target_url: str, token: str | None) -> str:
    wd = pathlib.Path(workdir).resolve()
    if (wd / ".git").exists():
        log(f"обновляю рабочую копию {wd}")
        run(["git", "fetch", "--depth", "1", "origin", branch], cwd=str(wd))
        run(["git", "reset", "--hard", f"origin/{branch}"], cwd=str(wd))
        run(["git", "clean", "-fd"], cwd=str(wd))
        return str(wd)

    log(f"клонирую {target_url or workdir} -> {wd}")
    wd.parent.mkdir(parents=True, exist_ok=True)
    url = auth_url(target_url, token)
    res = subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, url, str(wd)],
                         capture_output=True, text=True)
    if res.returncode != 0:  # пустой репозиторий или нет такой ветки
        subprocess.run(["git", "clone", url, str(wd)], check=True, capture_output=True, text=True)
        run(["git", "checkout", "-B", branch], cwd=str(wd))
    return str(wd)


# --------------------------------------------------------------------------- #
# состояние
# --------------------------------------------------------------------------- #
def load_state(path: str) -> dict:
    if path == "none":
        return {}
    p = pathlib.Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(path: str, state: dict) -> None:
    if path == "none":
        return
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def upstream_commit(repo: str, path: str, token: str | None) -> dict:
    try:
        url = f"{API}/repos/{repo}/commits?path={urllib.parse.quote(path)}&per_page=1"
        data = json.loads(http_get(url, token))
        if isinstance(data, list) and data:
            c = data[0]
            return {"sha": c["sha"][:10],
                    "date": c["commit"]["committer"]["date"],
                    "message": c["commit"]["message"].splitlines()[0][:80]}
    except Exception as exc:  # noqa: BLE001
        log(f"   (инфо о коммите апстрима недоступно: {exc})")
    return {}


# --------------------------------------------------------------------------- #
# синхронизация ассетов (свои копии rayfield / drawing / translations)
# --------------------------------------------------------------------------- #
def sync_assets(assets: list[dict], wd: str) -> list[tuple[str, str]]:
    """Обновить файлы в рабочей копии. Возвращает список изменений."""
    changes: list[tuple[str, str]] = []
    for a in assets or []:
        if a.get("enabled") is False:
            continue
        name, url, dest_rel = a.get("name", a.get("to", "asset")), a["url"], a["to"]
        try:
            data = http_get(url, binary=True)
        except Exception as exc:  # noqa: BLE001
            log(f"   ! ассет {name}: не скачался ({exc}) — оставляю старый файл")
            continue
        dest = pathlib.Path(wd) / dest_rel
        old = dest.read_bytes() if dest.exists() else None
        if old == data:
            log(f"   = ассет {name}: актуален ({len(data)} б)")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        was = f"было {len(old)} б, " if old is not None else "файла не было, "
        changes.append((dest_rel, f"{was}стало {len(data)} б"))
        log(f"   ↑ ассет {name}: обновлён -> {dest_rel} ({was}стало {len(data)} б)")
    return changes


# --------------------------------------------------------------------------- #
# основной цикл
# --------------------------------------------------------------------------- #
def sync(cfg, token: str | None) -> str:
    log("проверяю апстрим…")
    try:
        raw = http_get(cfg.source_url, binary=True)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"апстрим недоступен: HTTP {exc.code}") from exc

    src_sha = hashlib.sha256(raw).hexdigest()
    state = load_state(cfg.state)
    script_changed = state.get("source_sha") != src_sha or cfg.force
    has_assets = bool([a for a in cfg.assets or [] if a.get("enabled") is not False])

    if not script_changed and not has_assets and not cfg.force:
        log(f"обновлений нет (sha256 {src_sha[:12]}, {len(raw)} б)")
        return "up-to-date"
    if not script_changed:
        log(f"скрипт не менялся (sha256 {src_sha[:12]}), проверяю ассеты…")
    else:
        log(f"исходник обновился! sha256 {src_sha[:12]}, {len(raw)} б "
            f"(было {str(state.get('source_sha'))[:12]})")

    # --- рабочая копия твоего репозитория -------------------------------- #
    wd = None
    if not cfg.dry_run:
        if cfg.local_dir:
            wd = str(pathlib.Path(cfg.local_dir).resolve())
            log(f"работаю в готовом checkout: {wd}")
        else:
            if not cfg.target_repo and not cfg.target_url:
                raise RuntimeError("не задан --target-repo (напр. myuser/skidsaken) или --target-url")
            target_url = cfg.target_url or f"https://github.com/{cfg.target_repo}.git"
            wd = ensure_worktree(cfg.workdir, cfg.target_branch, target_url, token)
    else:
        wd = cfg.local_dir or (cfg.workdir if (pathlib.Path(cfg.workdir) / ".git").exists() else None)

    # --- ассеты ---------------------------------------------------------- #
    asset_changes: list[tuple[str, str]] = []
    if has_assets:
        log("синхронизирую свои копии библиотек…")
        if wd:
            asset_changes = sync_assets(cfg.assets, wd)
        else:
            for a in [x for x in cfg.assets if x.get("enabled") is not False]:
                try:
                    size = len(http_get(a["url"], binary=True))
                    log(f"   ? ассет {a.get('name')}: доступен ({size} б), рабочей копии нет — сравнить не с чем")
                except Exception as exc:  # noqa: BLE001
                    log(f"   ! ассет {a.get('name')}: ошибка загрузки ({exc})")

    # --- правки исходника ------------------------------------------------ #
    if script_changed:
        try:
            source_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError("файл не UTF-8 — похоже на обфускацию/бинарь, прерываю")
        if len(source_text) < 2000:
            raise RuntimeError(f"подозрительно маленький файл ({len(source_text)} симв.), не пушу")

        url_rules = [tuple(r.split("=", 1)) for r in cfg.rewrite_url]
        if cfg.telemetry_url:
            url_rules += [("https://tamper.chieokure.workers.dev/", cfg.telemetry_url),
                          ("https://catsaken.chieokure.workers.dev/", cfg.telemetry_url)]

        def exists_fn(rel: str) -> bool:
            if not wd:            # dry-run без рабочей копии: считаем, что файлы есть
                return True
            return (pathlib.Path(wd) / rel).exists()

        result, stats, patch_report = transform(
            source_text, cfg.src_name, cfg.dst_name,
            url_rules=url_rules,
            attribution=cfg.source_url if cfg.attribution else None,
            patches=cfg.patches,
            exists_fn=exists_fn,
            log=log,
        )
        log(f"переименование: {stats}")
        if patch_report["failed_required"] and not cfg.ignore_required:
            raise RuntimeError("обязательные патчи не сработали: "
                               + ", ".join(patch_report["failed_required"])
                               + " — апстрим поменялся, поправь конфиг (или --ignore-required)")
        info = upstream_commit(cfg.source_repo, cfg.source_path, token)
        if info:
            log(f"апстрим-коммит {info['sha']} от {info['date']}: {info['message']}")
    else:
        result, stats, patch_report, info = None, None, None, upstream_commit(
            cfg.source_repo, cfg.source_path, token)

    # --- dry-run --------------------------------------------------------- #
    if cfg.dry_run:
        out = pathlib.Path(cfg.target_file).with_suffix(".preview.lua")
        if result is not None:
            out.write_text(result, encoding="utf-8")
            log(f"DRY-RUN: ничего не пушу, результат в {out}")
        else:
            log("DRY-RUN: правки исходника не требуется")
        return "dry-run"

    # --- запись, коммит, пуш --------------------------------------------- #
    if result is not None:
        dest = pathlib.Path(wd) / cfg.target_file
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(result, encoding="utf-8")

    paths = ([cfg.target_file] if result is not None else []) + [to for to, _ in asset_changes]
    if paths:
        run(["git", "add", "--"] + paths, cwd=wd)

    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=wd).returncode == 0:
        log("изменений в файлах нет — коммит не нужен")
        save_state(cfg.state, {"source_sha": src_sha, "upstream": info,
                               "synced_at": dt.datetime.now().isoformat(timespec="seconds")})
        return "up-to-date"

    numstat = run(["git", "diff", "--cached", "--numstat"], cwd=wd, quiet=True).stdout.strip()
    head = (f"sync {cfg.dst_name}: апстрим {info.get('sha', 'unknown')}"
            f" ({info.get('date', 'n/a')})" if result is not None
            else f"sync {cfg.dst_name}: обновление библиотек")
    body_lines = [head, ""]
    if result is not None:
        body_lines.append(info.get("message", cfg.source_path))
        body_lines += [f"заменено вхождений: {stats.replaced}; URL сохранено: {stats.urls_kept}"]
        if patch_report and patch_report["applied"]:
            body_lines.append("патчи: " + "; ".join(f"{n}×{c}" for n, c in patch_report["applied"] if c))
    if asset_changes:
        body_lines.append("ассеты: " + "; ".join(f"{n} ({d})" for n, d in asset_changes))
    body_lines += [numstat, f"источник: {cfg.source_url}"]

    run(["git", "-c", f"user.name={cfg.git_name}", "-c", f"user.email={cfg.git_email}",
         "commit", "-m", "\n".join(body_lines)], cwd=wd)
    log(f"коммит: {run(['git', 'rev-parse', '--short', 'HEAD'], cwd=wd, quiet=True).stdout.strip()}")

    if cfg.local_dir:
        push_url, push_to = "origin", "origin (checkout)"
    else:
        target_url = cfg.target_url or f"https://github.com/{cfg.target_repo}.git"
        push_url, push_to = auth_url(target_url, token), (cfg.target_repo or target_url)
    log(f"пушу в {push_to} ({cfg.target_branch})")
    run(["git", "push", push_url, f"HEAD:{cfg.target_branch}"], cwd=wd)

    save_state(cfg.state, {
        "source_sha": src_sha,
        "upstream": info,
        "synced_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stats": {"replaced": stats.replaced, "urls_kept": stats.urls_kept} if stats else {},
        "assets": {n: d for n, d in asset_changes},
    })
    log("готово ✅")
    return "pushed"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Следить за cat.lua, переименовать catsaken -> skidsaken и запушить в свой репозиторий",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", default="", metavar="PATH", help="JSON-конфиг (см. config.megasaken.json)")
    # значения ниже по умолчанию None -> берутся из конфига, затем из DEFAULTS
    p.add_argument("--source-url", default=None)
    p.add_argument("--source-repo", default=None)
    p.add_argument("--source-path", default=None)
    p.add_argument("--src-name", default=None, help="что заменяем")
    p.add_argument("--dst-name", default=None, help="на что заменяем")
    p.add_argument("--target-repo", default=None, help="твой репозиторий owner/repo")
    p.add_argument("--target-url", default=None, help="свой git remote вместо --target-repo")
    p.add_argument("--target-branch", default=None)
    p.add_argument("--target-file", default=None, help="имя файла в твоём репозитории")
    p.add_argument("--workdir", default=None, help="локальная рабочая копия")
    p.add_argument("--local-dir", default=None, help="использовать готовый checkout (для CI)")
    p.add_argument("--state", default=None, help="файл состояния или 'none'")
    p.add_argument("--interval", type=int, default=None, help="период для --watch, сек")
    p.add_argument("--attribution", action=argparse.BooleanOptionalAction, default=None,
                   help="добавлять в начало файла шапку со ссылкой на оригинал")
    p.add_argument("--telemetry-url", default=None, metavar="URL",
                   help="куда слать 'звонки домой' оригинала (по умолчанию как есть)")
    p.add_argument("--rewrite-url", action="append", default=None, metavar="OLD=NEW",
                   help="разрешить замену и внутри URL")
    p.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""), help="PAT ($GITHUB_TOKEN)")
    p.add_argument("--token-file", default="", metavar="PATH", help="файл с токеном (для cron, chmod 600)")
    p.add_argument("--git-name", default=None)
    p.add_argument("--git-email", default=None)
    p.add_argument("--once", action="store_true", help="одна проверка и выход")
    p.add_argument("--watch", action="store_true", help="работать в цикле")
    p.add_argument("--dry-run", action="store_true", help="ничего не пушить")
    p.add_argument("--force", action="store_true", help="синхронизировать даже при совпадении хеша")
    p.add_argument("--ignore-required", action="store_true",
                   help="не прерывать синк, если обязательный патч не сработал")
    return p


def load_config(args) -> dict:
    conf: dict = {}
    if args.config:
        path = pathlib.Path(args.config)
        if not path.exists():
            raise SystemExit(f"конфиг не найден: {path}")
        conf = json.loads(path.read_text(encoding="utf-8"))
        for key in list(conf):
            if key.startswith("_"):  # комментарии в конфиге
                conf.pop(key)
    return conf


def resolve_cfg(args, conf: dict):
    cfg = argparse.Namespace(**vars(args))
    for key in CONFIG_KEYS:
        cli_val = getattr(args, key.replace("-", "_"), None)
        conf_val = conf.get(key)
        setattr(cfg, key, cli_val if cli_val is not None else
                (conf_val if conf_val is not None else DEFAULTS[key]))
    if args.rewrite_url:  # флаги дополняют конфиг
        cfg.rewrite_url = list(args.rewrite_url) + list(cfg.rewrite_url or [])
    return cfg


def main() -> int:
    args = build_parser().parse_args()
    conf = load_config(args)
    cfg = resolve_cfg(args, conf)

    if not cfg.token and cfg.token_file:
        cfg.token = pathlib.Path(cfg.token_file).read_text(encoding="utf-8").strip()
    token = cfg.token or None

    if not cfg.once and not cfg.watch:
        cfg.once = True
    while True:
        try:
            status = sync(cfg, token)
        except Exception as exc:  # noqa: BLE001
            log(f"ОШИБКА: {exc}")
            status = "error"
            if cfg.once:
                return 1
        if cfg.once:
            return 0 if status != "error" else 1
        log(f"сплю {cfg.interval} с… (Ctrl+C — выход)")
        try:
            time.sleep(cfg.interval)
        except KeyboardInterrupt:
            log("выхожу")
            return 0


if __name__ == "__main__":
    sys.exit(main())
