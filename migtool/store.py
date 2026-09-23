"""JSON 文档存储层。

数据目录结构::

    <store>/
      records/<id>.json      每条记录一个 JSON 文件
      indexes/<name>.json    索引文件 {"field": ..., "map": {value: [id, ...]}}
      .migration/            迁移状态、快照与审计日志（由 engine 管理）

所有写入均为「临时文件 + fsync + os.replace」的原子写，
保证进程在任意时刻被强杀都不会留下半写的文件。
"""

import json
import os
import tempfile
from pathlib import Path


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.records_dir = self.root / "records"
        self.index_dir = self.root / "indexes"
        self.meta_dir = self.root / ".migration"
        for d in (self.records_dir, self.index_dir, self.meta_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- 记录 ----
    def _record_path(self, rid: str) -> Path:
        return self.records_dir / f"{rid}.json"

    def list_ids(self):
        return sorted(p.stem for p in self.records_dir.glob("*.json"))

    def count(self) -> int:
        return len(self.list_ids())

    def read(self, rid: str) -> dict:
        with open(self._record_path(rid), encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, rid: str, doc: dict) -> None:
        _atomic_write_json(self._record_path(rid), doc)

    def delete(self, rid: str) -> None:
        try:
            os.unlink(self._record_path(rid))
        except FileNotFoundError:
            pass

    # ---- 索引 ----
    def _index_path(self, name: str) -> Path:
        return self.index_dir / f"{name}.json"

    def index_exists(self, name: str) -> bool:
        return self._index_path(name).exists()

    def read_index(self, name: str) -> dict:
        with open(self._index_path(name), encoding="utf-8") as fh:
            return json.load(fh)

    def rebuild_index(self, name: str, field: str) -> int:
        """全量重建索引，返回索引键数。原子替换，可安全重入。"""
        mapping = {}
        for rid in self.list_ids():
            doc = self.read(rid)
            if field in doc and doc[field] is not None:
                mapping.setdefault(str(doc[field]), []).append(rid)
        _atomic_write_json(self._index_path(name), {"field": field, "map": mapping})
        return len(mapping)

    def drop_index(self, name: str) -> None:
        try:
            os.unlink(self._index_path(name))
        except FileNotFoundError:
            pass
