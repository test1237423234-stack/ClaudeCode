#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import pathlib
import sys

DEFAULT_CONFIG = "config.megasaken.json"


def rewrite(node, old_repo: str, new_repo: str):
    """Рекурсивно заменить old_repo -> new_repo во всех строках (кроме комментариев)."""
    hits = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).startswith("_comment"):
                continue
            if isinstance(v, str):
                if old_repo in v:
                    hits += v.count(old_repo)
                    node[k] = v.replace(old_repo, new_repo)
            else:
                hits += rewrite(v, old_repo, new_repo)
    elif isinstance(node, list):
        for item in node:
            hits += rewrite(item, old_repo, new_repo)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Переключить зеркало на другой репозиторий")
    ap.add_argument("new_repo", help="владелец/репозиторий, напр. test1237423234-stack/MegaSakenTest")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()

    if "/" not in args.new_repo:
        print("Укажи в виде владелец/репозиторий", file=sys.stderr)
        return 2

    path = pathlib.Path(args.config)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    old_repo = cfg.get("target_repo") or ""

    if old_repo and old_repo != args.new_repo:
        hits = rewrite(cfg, old_repo, args.new_repo)
        print(f"ссылок обновлено: {hits}")
    elif not old_repo:
        print("в конфиге не было target_repo — просто прописываю новый")
    else:
        print("конфиг уже настроен на этот репозиторий")

    cfg["target_repo"] = args.new_repo
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"target_repo = {args.new_repo}")
    print("ссылки в патчах, которые теперь указывают на новый репозиторий:")
    for patch in cfg.get("patches", []):
        for field in ("from", "to", "pattern"):
            v = patch.get(field) or ""
            if args.new_repo in v:
                print(f"  • {patch.get('name', patch.get('type'))}: {field}")
    print("\nНе забудь: в новом репозитории должны лежать Skidsakenlogo.png и "
          "SkidsakenBackground.png (иначе картинки останутся с репо автора).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
