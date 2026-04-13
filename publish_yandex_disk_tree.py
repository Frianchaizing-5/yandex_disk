#!/usr/bin/env python3
"""
Рекурсивно публикует все файлы и папки внутри указанного каталога на Яндекс.Диске
и записывает CSV: путь, тип, публичная ссылка (public_url).

Требуется OAuth с правом записи на Диск, например cloud_api:disk.write
(см. https://yandex.ru/dev/disk-api/doc/ru/concepts/quickstart).

Папка из URL client/disk/%D1%82%D0%B5%D1%81%D1%82 — это каталог «тест»:
  python publish_yandex_disk_tree.py --folder "тест" --out published.csv

Только корневая папка одной операцией (быстро, одна публичная ссылка на каталог):
  python publish_yandex_disk_tree.py --folder "тест" --root-only --out published_root.csv

Переменная окружения YANDEX_DISK_TOKEN или --token.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
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


def api_get_href(token: str, href: str) -> dict[str, Any]:
    req = urllib.request.Request(
        href,
        headers={
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_put_publish(token: str, disk_path: str) -> tuple[int, dict[str, Any] | None]:
    q = urllib.parse.urlencode({"path": disk_path})
    url = f"{API}/disk/resources/publish?{q}"
    req = urllib.request.Request(
        url,
        data=b"",
        headers={
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
            code = resp.status
            if not body:
                return code, None
            return code, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        raise PublishHttpError(e.code, body, parsed) from e


class PublishHttpError(Exception):
    def __init__(self, code: int, body: str, parsed: dict[str, Any] | None) -> None:
        self.code = code
        self.body = body
        self.parsed = parsed
        super().__init__(f"HTTP {code}: {body[:500]}")


def wait_operation(token: str, href: str, poll: float) -> None:
    """Ожидает завершения асинхронной операции (ссылка из ответа publish)."""
    deadline = time.monotonic() + 600.0
    while time.monotonic() < deadline:
        data = api_get_href(token, href)
        status = data.get("status")
        if status == "success":
            return
        if status == "failed":
            raise RuntimeError(f"Операция завершилась с ошибкой: {data}")
        time.sleep(poll)
    raise TimeoutError("Таймаут ожидания операции публикации")


def handle_publish_response(token: str, data: dict[str, Any] | None, poll: float) -> None:
    if not data:
        return
    href = data.get("href")
    if not href or "/operations/" not in str(href):
        return
    wait_operation(token, str(href), poll)


def publish_one(
    token: str, disk_path: str, poll: float
) -> None:
    try:
        code, body = api_put_publish(token, disk_path)
        if code in (200, 202) and body:
            handle_publish_response(token, body, poll)
    except PublishHttpError as e:
        if e.code == 409:
            return
        if e.code == 404:
            raise RuntimeError(f"Не найдено: {disk_path}") from e
        raise


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


def walk_publish_order(token: str, disk_path: str) -> list[tuple[str, str]]:
    """
    Порядок: сначала вложенные файлы и папки (глубина), затем родительские папки.
    Сам корень disk_path в список не входит — его добавляем снаружи.
    """
    ordered: list[tuple[str, str]] = []
    items = sorted(
        list(iter_folder_items(token, disk_path)),
        key=lambda x: (x.get("name") or "").lower(),
    )
    for item in items:
        name = item.get("name") or ""
        item_type = item.get("type")
        child = item.get("path") or f"{disk_path.rstrip('/')}/{name}"
        if item_type == "dir":
            ordered.extend(walk_publish_order(token, child))
            ordered.append((child, "dir"))
        elif item_type == "file":
            ordered.append((child, "file"))
        else:
            print(f"Пропуск неизвестного типа: {child}", file=sys.stderr)
    return ordered


def fetch_public_url(token: str, disk_path: str) -> str:
    meta = api_get(token, "/disk/resources", {"path": disk_path})
    u = meta.get("public_url")
    return str(u) if u else ""


def fetch_public_url_retry(
    token: str, disk_path: str, retries: int = 5, pause: float = 0.4
) -> str:
    """Иногда public_url появляется не с первого GET после publish."""
    last = ""
    for _ in range(retries):
        last = fetch_public_url(token, disk_path)
        if last:
            return last
        time.sleep(pause)
    return last


def main() -> int:
    p = argparse.ArgumentParser(
        description="Публикация всех файлов и папок в каталоге Яндекс.Диска + CSV с ссылками."
    )
    p.add_argument(
        "--token",
        default=os.environ.get("YANDEX_DISK_TOKEN", ""),
        help="OAuth-токен или YANDEX_DISK_TOKEN",
    )
    p.add_argument(
        "--folder",
        default="тест",
        help='Каталог на Диске (как в URL client/disk/...). По умолчанию: "тест"',
    )
    p.add_argument(
        "--out",
        default="published.csv",
        help="CSV: path;type;public_url",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="Пауза между запросами (сек), снижает риск лимитов API",
    )
    p.add_argument(
        "--poll",
        type=float,
        default=0.5,
        help="Интервал опроса асинхронной операции (сек)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Только вывести порядок путей, без публикации",
    )
    p.add_argument(
        "--root-only",
        action="store_true",
        help="Публиковать только указанную папку (без обхода файлов и подпапок)",
    )
    args = p.parse_args()

    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not args.token:
        print("Нужен токен: YANDEX_DISK_TOKEN или --token", file=sys.stderr)
        return 1

    root = normalize_disk_path(args.folder)
    rows: list[dict[str, str]] = []

    if args.root_only:
        ordered = [(root, "dir")]
    else:
        try:
            print("Сканирование дерева (только чтение метаданных)...", flush=True)
            ordered = walk_publish_order(args.token, root)
            ordered.append((root, "dir"))
            print(f"К публикации: {len(ordered)} объектов.", flush=True)
        except urllib.error.HTTPError as e:
            print(
                f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}",
                file=sys.stderr,
            )
            return 1

    if args.dry_run:
        for path, typ in ordered:
            print(f"{typ}\t{path}", flush=True)
        print(f"Всего: {len(ordered)}", file=sys.stderr, flush=True)
        return 0

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as out_f:
        w = csv.DictWriter(
            out_f,
            fieldnames=["path", "type", "public_url"],
            delimiter=";",
        )
        w.writeheader()
        out_f.flush()

        for disk_path, typ in ordered:
            try:
                publish_one(args.token, disk_path, args.poll)
            except PublishHttpError as e:
                print(f"Ошибка publish {disk_path}: {e}", file=sys.stderr, flush=True)
                if e.parsed:
                    print(
                        json.dumps(e.parsed, ensure_ascii=False),
                        file=sys.stderr,
                        flush=True,
                    )
                return 1
            time.sleep(args.delay)
            pub = fetch_public_url_retry(args.token, disk_path)
            row = {"path": disk_path, "type": typ, "public_url": pub}
            rows.append(row)
            w.writerow(row)
            out_f.flush()
            print(f"{typ}\t{disk_path}", flush=True)

    print(f"Готово: {len(rows)} объектов -> {out_path}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
