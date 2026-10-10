"""Pasted folder paths must resolve correctly without bypassing source safety."""
import pytest

from app.config import ConfigError, RuntimeConfig, assert_safe_paths
from app.pipeline import LocalSource


@pytest.mark.parametrize("wrapper", ["", '"', "'"])
def test_pasted_folder_path_scans_real_folder(tmp_path, wrapper):
    folder = tmp_path / "Ganesh sir's data"
    folder.mkdir()
    original = folder / "companies.csv"
    original.write_text("company,city\nAcme,Jaipur\n", encoding="utf-8")
    cfg = RuntimeConfig.from_dict({"local_input_dir": f"  {wrapper}{folder}{wrapper}  "})
    files = list(LocalSource(cfg.local_input_dir).scan())
    assert cfg.local_input_dir == str(folder)
    assert [file.name for file in files] == ["companies.csv"]
    assert original.read_text(encoding="utf-8") == "company,city\nAcme,Jaipur\n"


def test_quoted_windows_drive_path_is_not_a_relative_path():
    cfg = RuntimeConfig.from_dict({"local_input_dir": '"D:\\24-04-2026 backup\\ganesh sir data"'})
    assert cfg.local_input_dir == r"D:\24-04-2026 backup\ganesh sir data"


def test_quoted_paths_still_reject_overlapping_output(tmp_path):
    cfg = RuntimeConfig.from_dict({"local_input_dir": f'"{tmp_path}"', "output_dir": f'"{tmp_path / "out"}"'})
    with pytest.raises(ConfigError, match="overlaps"):
        assert_safe_paths(cfg.local_input_dir, cfg.output_dir)
