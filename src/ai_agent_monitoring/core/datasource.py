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
    次回以降の自動選択に使用する。複数UID（list[str]）に対応。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, Any]:
        """プリファレンスを読み込み."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, dict):
                return dict(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load datasource preferences: %s", e)
        return {}

    def save(self, ds_type: str, uid: str) -> None:
        """プリファレンスを保存（単一UID、後方互換）."""
        self.save_uids(ds_type, [uid])

    def save_uids(self, ds_type: str, uids: list[str]) -> None:
        """プリファレンスを保存（複数UID）."""
        prefs = self.load()
        prefs[ds_type] = uids
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2))
            logger.info("Saved datasource preference: %s=%s", ds_type, uids)
        except OSError as e:
            logger.warning("Failed to save datasource preferences: %s", e)

    def get_preferred_uid(self, ds_type: str) -> str:
        """指定タイプの優先UIDを取得（後方互換: 先頭を返す）."""
        uids = self.get_preferred_uids(ds_type)
        return uids[0] if uids else ""

    def get_preferred_uids(self, ds_type: str) -> list[str]:
        """指定タイプの優先UID一覧を取得.

        旧フォーマット（str値）の読み込み互換を維持。
        """
        raw = self.load().get(ds_type)
        if raw is None:
            return []
        # 旧フォーマット: str → list[str] に変換
        if isinstance(raw, str):
            return [raw] if raw else []
        if isinstance(raw, list):
            return [str(u) for u in raw if u]
        return [str(raw)] if raw else []


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
    """候補からデータソースを選択（単一、後方互換）.

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


def select_datasources(
    candidates: list[DatasourceInfo],
    preferred_uids: list[str] | None = None,
) -> list[DatasourceInfo]:
    """候補からデータソースを選択（複数対応）.

    preferred_uids の全UIDが候補に存在すればそれらを返す。
    一部でも見つからない場合は空リストを返す（呼び出し元でinterrupt判断）。
    preferred_uids が空の場合も空リストを返す。
    """
    if not candidates or not preferred_uids:
        return []

    uid_set = {ds.uid for ds in candidates}
    if all(uid in uid_set for uid in preferred_uids):
        uid_to_ds = {ds.uid: ds for ds in candidates}
        return [uid_to_ds[uid] for uid in preferred_uids]

    return []
