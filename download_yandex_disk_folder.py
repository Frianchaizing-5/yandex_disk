#!/usr/bin/env python3
"""
Рекурсивно скачивает папку с Яндекс.Диска через REST API.
Токен: https://yandex.ru/dev/disk-api/doc/ru/concepts/quickstart

Переменная окружения YANDEX_DISK_TOKEN обязательна (или --token).
Пример:
  set YANDEX_DISK_TOKEN=ваш_токен
  python download_yandex_disk_folder.py --folder "ВКС записи" --out ./backup
"""

from __future__ import annotations

import argparse
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
    """disk_path вида disk:/foo/bar или /foo/bar."""
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


def get_download_href(token: str, disk_path: str) -> str:
    data = api_get(token, "/disk/resources/download", {"path": disk_path})
    href = data.get("href")
    if not href:
        raise RuntimeError(f"Нет ссылки на скачивание: {data}")
    return str(href)


def download_url_to_file(url: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = resp.read()
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, dest)


def normalize_disk_path(folder: str) -> str:
    folder = folder.strip().strip("/")
    if not folder.startswith("disk:"):
        return "disk:/" + folder
    return folder


def walk_and_download(
    token: str,
    disk_path: str,
    local_root: str,
    rel: str,
    dry_run: bool,
) -> None:
    for item in iter_folder_items(token, disk_path):
        name = item.get("name") or ""
        item_type = item.get("type")
        child_disk = item.get("path") or f"{disk_path.rstrip('/')}/{name}"
        child_rel = os.path.join(rel, name) if rel else name
        local_path = os.path.join(local_root, child_rel)

        if item_type == "dir":
            walk_and_download(token, child_disk, local_root, child_rel, dry_run)
        elif item_type == "file":
            if dry_run:
                print(f"[dry-run] {child_disk}")
                continue
            href = get_download_href(token, child_disk)
            print(f"Скачиваю: {child_rel}")
            download_url_to_file(href, local_path)
        else:
            print(f"Пропуск (неизвестный type={item_type}): {child_disk}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description="Выгрузка папки с Яндекс.Диска (REST API).")
    p.add_argument(
        "--token",
        default=os.environ.get("YANDEX_DISK_TOKEN", ""),
        help="OAuth-токен (или переменная YANDEX_DISK_TOKEN)",
    )
    p.add_argument(
        "--folder",
        default="ВКС записи",
        help='Имя папки в корне Диска (как в URL client/disk/...). По умолчанию: "ВКС записи"',
    )
    p.add_argument(
        "--out",
        default="yandex_export",
        help="Локальная папка для сохранения",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Только вывести пути файлов, без скачивания",
    )
    args = p.parse_args()

    if not args.token:
        print(
            "Укажите токен: переменная YANDEX_DISK_TOKEN или --token",
            file=sys.stderr,
        )
        return 1

    disk_root = normalize_disk_path(args.folder)
    local_root = os.path.abspath(args.out)
    print(f"Папка на Диске: {disk_root}")
    print(f"Сохранение в:   {local_root}")

    try:
        walk_and_download(args.token, disk_root, local_root, "", args.dry_run)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
