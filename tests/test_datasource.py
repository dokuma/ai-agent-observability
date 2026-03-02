"""datasource モジュールのテスト."""

from pathlib import Path

from ai_agent_monitoring.core.datasource import (
    DatasourceInfo,
    DatasourcePreferenceStore,
    parse_datasource_list,
    select_datasource,
)

# ---- DatasourcePreferenceStore ----


class TestDatasourcePreferenceStore:
    """DatasourcePreferenceStore のテスト."""

    def test_load_empty_when_file_not_exists(self, tmp_path: Path):
        """ファイルが存在しない場合は空dictを返す."""
        store = DatasourcePreferenceStore(tmp_path / "nonexistent.json")
        assert store.load() == {}

    def test_save_and_load(self, tmp_path: Path):
        """保存した値が読み込める."""
        path = tmp_path / "prefs.json"
        store = DatasourcePreferenceStore(path)

        store.save("prometheus", "uid-prom-1")
        assert store.load() == {"prometheus": "uid-prom-1"}

        store.save("loki", "uid-loki-1")
        prefs = store.load()
        assert prefs == {"prometheus": "uid-prom-1", "loki": "uid-loki-1"}

    def test_get_preferred_uid(self, tmp_path: Path):
        """get_preferred_uidで正しいUIDが返る."""
        path = tmp_path / "prefs.json"
        store = DatasourcePreferenceStore(path)

        assert store.get_preferred_uid("prometheus") == ""

        store.save("prometheus", "uid-prom-1")
        assert store.get_preferred_uid("prometheus") == "uid-prom-1"
        assert store.get_preferred_uid("loki") == ""

    def test_load_corrupted_file(self, tmp_path: Path):
        """破損ファイルの場合は空dictを返す."""
        path = tmp_path / "prefs.json"
        path.write_text("not valid json {{{")

        store = DatasourcePreferenceStore(path)
        assert store.load() == {}

    def test_save_creates_parent_directories(self, tmp_path: Path):
        """親ディレクトリが存在しない場合は自動作成."""
        path = tmp_path / "subdir" / "deep" / "prefs.json"
        store = DatasourcePreferenceStore(path)

        store.save("prometheus", "uid-1")
        assert path.exists()
        assert store.get_preferred_uid("prometheus") == "uid-1"

    def test_overwrite_existing_preference(self, tmp_path: Path):
        """既存のプリファレンスを上書きできる."""
        path = tmp_path / "prefs.json"
        store = DatasourcePreferenceStore(path)

        store.save("prometheus", "uid-old")
        store.save("prometheus", "uid-new")
        assert store.get_preferred_uid("prometheus") == "uid-new"


# ---- parse_datasource_list ----


class TestParseDatasourceList:
    """parse_datasource_list のテスト."""

    def test_normal_parse(self):
        """正常なリストをパースできる."""
        raw = [
            {
                "uid": "prom-1",
                "name": "Prometheus Main",
                "type": "prometheus",
                "isDefault": True,
                "url": "http://prom:9090",
            },
            {"uid": "loki-1", "name": "Loki", "type": "loki", "isDefault": False, "url": "http://loki:3100"},
        ]
        result = parse_datasource_list(raw)
        assert len(result) == 2
        assert result[0].uid == "prom-1"
        assert result[0].name == "Prometheus Main"
        assert result[0].type == "prometheus"
        assert result[0].is_default is True
        assert result[1].uid == "loki-1"
        assert result[1].is_default is False

    def test_empty_list(self):
        """空リストを処理できる."""
        assert parse_datasource_list([]) == []

    def test_missing_fields(self):
        """フィールド不足でもパースできる（デフォルト値使用）."""
        raw = [{"uid": "ds-1"}]
        result = parse_datasource_list(raw)
        assert len(result) == 1
        assert result[0].uid == "ds-1"
        assert result[0].name == ""
        assert result[0].type == ""
        assert result[0].is_default is False

    def test_missing_uid(self):
        """uidが空でもパースできる."""
        raw = [{"name": "No UID", "type": "prometheus"}]
        result = parse_datasource_list(raw)
        assert len(result) == 1
        assert result[0].uid == ""


# ---- select_datasource ----


class TestSelectDatasource:
    """select_datasource のテスト."""

    def _make_ds(self, uid: str, name: str = "", is_default: bool = False) -> DatasourceInfo:
        return DatasourceInfo(uid=uid, name=name, type="prometheus", is_default=is_default)

    def test_empty_candidates(self):
        """空候補ではNoneを返す."""
        assert select_datasource([]) is None

    def test_single_candidate(self):
        """単一候補はそのまま返す."""
        ds = self._make_ds("uid-1")
        assert select_datasource([ds]) == ds

    def test_preference_match(self):
        """プリファレンス一致で選択."""
        ds1 = self._make_ds("uid-1", "DS 1")
        ds2 = self._make_ds("uid-2", "DS 2")
        result = select_datasource([ds1, ds2], preferred_uid="uid-2")
        assert result == ds2

    def test_preference_not_found_falls_to_default(self):
        """プリファレンスが見つからない場合はisDefaultにフォールバック."""
        ds1 = self._make_ds("uid-1", "DS 1", is_default=False)
        ds2 = self._make_ds("uid-2", "DS 2", is_default=True)
        result = select_datasource([ds1, ds2], preferred_uid="uid-nonexistent")
        assert result == ds2

    def test_is_default_fallback(self):
        """プリファレンスなしでisDefaultにフォールバック."""
        ds1 = self._make_ds("uid-1", "DS 1")
        ds2 = self._make_ds("uid-2", "DS 2", is_default=True)
        result = select_datasource([ds1, ds2])
        assert result == ds2

    def test_fallback_to_first(self):
        """プリファレンスもisDefaultもない場合は先頭にフォールバック."""
        ds1 = self._make_ds("uid-1", "DS 1")
        ds2 = self._make_ds("uid-2", "DS 2")
        result = select_datasource([ds1, ds2])
        assert result == ds1
