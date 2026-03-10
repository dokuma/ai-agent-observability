"""データソース選択・プリファレンス管理."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class DatasourceInfo(BaseModel):
    """Grafana データソース情報."""

    uid: str
    name: str = ""
    type: str = ""  # "prometheus" | "loki"
    is_default: bool = False
    url: str = ""


class DatasourcePreferenceStore:
    """データソースプリファレンスの永続化ストア.

    選択されたデータソースUIDをJSON形式でファイルに保存し、
    次回以降の自動選択に使用する。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, str]:
        """プリファレンスを読み込み."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, dict):
                return {k: str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load datasource preferences: %s", e)
        return {}

    def save(self, ds_type: str, uid: str) -> None:
        """プリファレンスを保存."""
        prefs = self.load()
        prefs[ds_type] = uid
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2))
            logger.info("Saved datasource preference: %s=%s", ds_type, uid)
        except OSError as e:
            logger.warning("Failed to save datasource preferences: %s", e)

    def get_preferred_uid(self, ds_type: str) -> str:
        """指定タイプの優先UIDを取得."""
        return self.load().get(ds_type, "")


def parse_datasource_list(raw: list[dict[str, Any]]) -> list[DatasourceInfo]:
    """生のデータソースリストをDatasourceInfoに変換."""
    result: list[DatasourceInfo] = []
    for item in raw:
        try:
            result.append(
                DatasourceInfo(
                    uid=str(item.get("uid", "")),
                    name=str(item.get("name", "")),
                    type=str(item.get("type", "")),
                    is_default=bool(item.get("isDefault", False)),
                    url=str(item.get("url", "")),
                )
            )
        except Exception as e:
            logger.warning("Failed to parse datasource entry: %s: %s", item, e)
    return result


def select_datasource(
    candidates: list[DatasourceInfo],
    preferred_uid: str = "",
) -> DatasourceInfo | None:
    """候補からデータソースを選択.

    優先順位: 1.プリファレンス一致 → 2.先頭
    Grafana の isDefault は信頼しない（ユーザの意図と一致しない場合がある）。
    """
    if not candidates:
        return None

    # 1. プリファレンス一致
    if preferred_uid:
        for ds in candidates:
            if ds.uid == preferred_uid:
                return ds

    # 2. 先頭
    return candidates[0]
