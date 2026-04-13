#!/usr/bin/env python3
"""
Выгрузка списка файлов с Яндекс.Диска: каталог, имя, публичная ссылка (без скачивания файлов).

Публичная ссылка (public_url) есть только у опубликованных файлов/папок.
Если колонка пустая — файл не расшарен; опубликуйте в интерфейсе Диска или через API publish.

Токен: https://yandex.ru/dev/disk-api/doc/ru/concepts/quickstart
Права: cloud_api:disk.read (для publish при --publish нужен write)

Пример:
  set YANDEX_DISK_TOKEN=ваш_токен
  python export_yandex_disk_index.py --folder "ВКС записи" --out index.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

API = "https://cloud-api.yandex.net/v1"


def api_get(token: str, path: str, params: dict[str, str]) -> dict[str, Any]:
    q = urllib.parse.urlencode(params)
    url = f"{API}{path}?{q}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def iter_folder_items(
    token: str, disk_path: str, limit: int = 1000
) -> Iterator[dict[str, Any]]:
    offset = 0
    while True:
        data = api_get(
            token,
            "/disk/resources",
            {"path": disk_path, "limit": str(limit), "offset": str(offset)},
        )
        emb = data.get("_embedded") or {}
        items = emb.get("items") or []
        for it in items:
            yield it
        if len(items) < limit:
            break
        offset += limit


def normalize_disk_path(folder: str) -> str:
    folder = folder.strip().strip("/")
    if not folder.startswith("disk:"):
        return "disk:/" + folder
    return folder


def parent_catalog(file_disk_path: str) -> str:
    """disk:/a/b/c.txt -> disk:/a/b; disk:/f.txt -> disk:/"""
    p = file_disk_path.rstrip("/")
    parent = p.rsplit("/", 1)[0]
    if parent == "disk:":
        return "disk:/"
    return parent


def walk_files(
    token: str, disk_path: str
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yields (catalog_path, file_name, raw_item)."""
    for item in iter_folder_items(token, disk_path):
        name = item.get("name") or ""
        item_type = item.get("type")
        child_disk = item.get("path") or f"{disk_path.rstrip('/')}/{name}"

        if item_type == "dir":
            yield from walk_files(token, child_disk)
        elif item_type == "file":
            cat = parent_catalog(child_disk)
            yield (cat, name, item)
        else:
            print(f"Пропуск type={item_type}: {child_disk}", file=sys.stderr)


def public_url_for_file(
    token: str, disk_path: str, item: dict[str, Any], fetch_full: bool
) -> str:
    url = item.get("public_url")
    if url:
        return str(url)
    if not fetch_full:
        return ""
    meta = api_get(token, "/disk/resources", {"path": disk_path})
    u = meta.get("public_url")
    return str(u) if u else ""


def main() -> int:
    p = argparse.ArgumentParser(
        description="CSV: каталог, имя файла, публичная ссылка (без скачивания)."
    )
    p.add_argument(
        "--token",
        default=os.environ.get("YANDEX_DISK_TOKEN", ""),
        help="OAuth-токен или YANDEX_DISK_TOKEN",
    )
    p.add_argument(
        "--folder",
        default="ВКС записи",
        help="Корневая папка на Диске для обхода",
    )
    p.add_argument(
        "--out",
        default="yandex_disk_index.csv",
        help="Файл CSV (UTF-8 с BOM для Excel)",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="Для каждого файла дополнительно GET метаданных (точнее public_url)",
    )
    args = p.parse_args()

    if not args.token:
        print("Нужен токен: YANDEX_DISK_TOKEN или --token", file=sys.stderr)
        return 1

    root = normalize_disk_path(args.folder)
    out_path = os.path.abspath(args.out)

    rows: list[dict[str, str]] = []
    try:
        for catalog, fname, item in walk_files(args.token, root):
            fpath = item.get("path") or ""
            if not fpath:
                fpath = f"{catalog.rstrip('/')}/{fname}"
            pub = public_url_for_file(args.token, fpath, item, args.full)
            rows.append(
                {
                    "catalog": catalog,
                    "filename": fname,
                    "public_url": pub,
                }
            )
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        return 1

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["catalog", "filename", "public_url"],
            delimiter=";",
        )
        w.writeheader()
        w.writerows(rows)

    print(f"Записано строк: {len(rows)} -> {out_path}")
    if rows and not any(r["public_url"] for r in rows):
        print(
            "Публичных ссылок нет: файлы не опубликованы. "
            "Опубликуйте в Диске или используйте API PUT .../resources/publish",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
