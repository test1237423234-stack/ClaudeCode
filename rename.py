#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import re
from dataclasses import dataclass, field

_URL_BODY = r"""\bhttps?://[^\s"'()\[\]{}<>]+"""
URL_RE = re.compile(_URL_BODY, re.I)
_TOKEN_RE = re.compile("(" + _URL_BODY + ")", re.I)

# Маркер атрибуции, чтобы заголовок добавлялся только один раз
ATTRIBUTION_MARKER = "MIRROR-NOTICE"


@dataclass
class Stats:
    replaced: int = 0
    urls_kept: int = 0
    urls_rewritten: int = 0
    by_case: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        parts = [f"заменено вхождений: {self.replaced}"]
        if self.by_case:
            det = ", ".join(f"{k}:{v}" for k, v in sorted(self.by_case.items()))
            parts.append(f"({det})")
        parts.append(f"URL сохранено: {self.urls_kept}")
        if self.urls_rewritten:
            parts.append(f"URL переписано: {self.urls_rewritten}")
        return " | ".join(parts)


def _match_case(matched: str, dst: str) -> str:
    """catsaken->skidsaken, Catsaken->Skidsaken, CATSAKEN->SKIDSAKEN."""
    if matched.isupper():
        return dst.upper()
    if matched[:1].isupper():
        return dst[:1].upper() + dst[1:]
    return dst.lower()


def _rename_plain(text: str, src: str, dst: str, stats: Stats) -> str:
    pattern = re.compile(re.escape(src), re.I)

    def repl(m: re.Match) -> str:
        got = _match_case(m.group(0), dst)
        stats.replaced += 1
        stats.by_case[m.group(0)] = stats.by_case.get(m.group(0), 0) + 1
        return got

    return pattern.sub(repl, text)


def _rewrite_url(url: str, rules: list[tuple[str, str]], stats: Stats) -> str:
    out = url
    for old, new in rules:
        if old in out:
            out = out.replace(old, new)
    if out != url:
        stats.urls_rewritten += 1
    return out


def rename_text(
    text: str,
    src: str = "catsaken",
    dst: str = "skidsaken",
    url_rules: list[tuple[str, str]] | None = None,
) -> tuple[str, Stats]:
    """Переименовать src -> dst во всём тексте, кроме URL."""
    stats = Stats()
    out: list[str] = []
    for i, token in enumerate(_TOKEN_RE.split(text)):
        if i % 2:  # нечётные куски — это URL
            stats.urls_kept += 1
            if url_rules:
                out.append(_rewrite_url(token, url_rules, stats))
            else:
                out.append(token)
        else:
            out.append(_rename_plain(token, src, dst, stats))
    return "".join(out), stats


def attribution_header(upstream_url: str, upstream_name: str = "aibabylaugh/catsaken") -> str:
    return (
        f"--[[ {ATTRIBUTION_MARKER}\n"
        f"\tЭто автоматически собранное зеркало (fork) скрипта {upstream_name}.\n"
        f"\tОригинал: {upstream_url}\n"
        f"\tВесь код принадлежит автору оригинала; изменены только названия (catsaken -> skidsaken).\n"
        f"--]]\n"
    )


def add_attribution(text: str, upstream_url: str) -> str:
    if ATTRIBUTION_MARKER in text[:4096]:
        return text
    return attribution_header(upstream_url) + text


# --------------------------------------------------------------------------- #
# Движок патчей: точечные правки кода/ссылок ПОСЛЕ переименования
# --------------------------------------------------------------------------- #
def apply_patches(
    text: str,
    patches: list[dict],
    exists_fn=None,
    log=print,
) -> tuple[str, dict]:
    """Применить патчи из конфига.

    Форматы (JSON):
      {"name": "...", "type": "replace", "from": "...", "to": "..."}
      {"name": "...", "type": "regex",   "pattern": "...", "to": "..."}
      {"name": "...", "type": "insert_before", "anchor": "...", "text": "..."}

    Общие поля:
      "required": true      — патч обязателен: если не сработал, синк прерывается
      "expect": 3           — сколько вхождений ДОЛЖНО найтись (защита от частичного
                              совпадения: апстрим изменился -> синк прерывается)
      "requires_file": "x"  — применять только если файл x есть в рабочей копии
      "enabled": false      — выключить патч, не удаляя его из конфига
    """
    report = {"applied": [], "skipped": [], "failed_required": []}

    for patch in patches or []:
        if patch.get("_comment_only"):
            continue
        name = patch.get("name") or patch.get("type", "patch")
        if patch.get("enabled") is False:
            report["skipped"].append((name, "выключен в конфиге"))
            continue

        need = patch.get("requires_file")
        if need and exists_fn is not None and not exists_fn(need):
            report["skipped"].append((name, f"нет файла {need} в репозитории"))
            log(f"   ~ патч пропущен: {name} (в репозитории нет {need})")
            continue

        count = 0
        if patch.get("type", "replace") == "replace":
            src = patch["from"]
            count = text.count(src)
            if count:
                text = text.replace(src, patch.get("to", ""))
        elif patch["type"] == "regex":
            pattern = re.compile(patch["pattern"])
            text, count = pattern.subn(patch.get("to", ""), text)
        elif patch["type"] == "insert_before":
            anchor = patch["anchor"]
            if anchor in text:
                count = text.count(anchor)
                if patch.get("once", True):
                    text = text.replace(anchor, patch["text"] + anchor, 1)
                    count = 1
                else:
                    text = text.replace(anchor, patch["text"] + anchor)
        else:
            raise ValueError(f"неизвестный тип патча: {patch['type']}")

        expect = patch.get("expect")
        mismatch = expect is not None and count != expect
        if count and not mismatch:
            report["applied"].append((name, count))
            log(f"   + патч: {name} ({count})")
        elif mismatch:
            msg = f"сработало {count} раз, ожидалось {expect}"
            if patch.get("required"):
                report["failed_required"].append(f"{name} ({msg})")
                log(f"   ! ОБЯЗАТЕЛЬНЫЙ патч: {name} — {msg}")
            else:
                report["applied"].append((name, count))
                log(f"   ! патч {name}: {msg} — проверь глазами")
        else:
            if patch.get("required"):
                report["failed_required"].append(name)
                log(f"   ! ОБЯЗАТЕЛЬНЫЙ патч не сработал: {name}")
            else:
                report["applied"].append((name, 0))
                log(f"   ! патч не найден (не критично): {name}")

    return text, report


def transform(
    text: str,
    src: str = "catsaken",
    dst: str = "skidsaken",
    url_rules: list[tuple[str, str]] | None = None,
    attribution: str | None = None,
    patches: list[dict] | None = None,
    exists_fn=None,
    log=print,
) -> tuple[str, Stats, dict]:
    new_text, stats = rename_text(text, src, dst, url_rules)
    patch_report = {"applied": [], "skipped": [], "failed_required": []}
    if patches:
        new_text, patch_report = apply_patches(new_text, patches, exists_fn, log)
    if attribution:
        new_text = add_attribution(new_text, attribution)
    return new_text, stats, patch_report


if __name__ == "__main__":
    import argparse
    import pathlib

    ap = argparse.ArgumentParser(description="catsaken -> skidsaken (тест/ручной прогон)")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--src", default="catsaken")
    ap.add_argument("--dst", default="skidsaken")
    ap.add_argument("--rewrite-url", action="append", default=[], metavar="OLD=NEW")
    ap.add_argument("--no-attribution", action="store_true")
    args = ap.parse_args()

    rules = [tuple(r.split("=", 1)) for r in args.rewrite_url]
    raw = pathlib.Path(args.input).read_text(encoding="utf-8", errors="surrogateescape")
    result, st = transform(
        raw,
        args.src,
        args.dst,
        rules,
        attribution=None if args.no_attribution else args.input,
    )
    print(st)
    if args.output:
        pathlib.Path(args.output).write_text(result, encoding="utf-8")
        print("записано:", args.output)
