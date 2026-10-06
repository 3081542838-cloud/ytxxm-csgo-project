from pathlib import Path
import pytest
from cs2pov.adapters.disk import MIN_VIDEO_BYTES, DiskError, check_directory


@pytest.mark.parametrize("free,allowed", [(9_999_999_999, False), (10_000_000_000, True), (10_000_000_001, True)])
def test_exact_decimal_boundary(tmp_path, free, allowed):
    probes = []
    if allowed:
        result = check_directory(tmp_path, query=lambda p: free, probe=probes.append)
        assert result.available_bytes == free
        assert probes == [tmp_path.resolve()]
    else:
        with pytest.raises(DiskError):
            check_directory(tmp_path, query=lambda p: free, probe=probes.append)
        assert probes == []


@pytest.mark.parametrize("free", [-1, None, 1.5, True])
def test_invalid_query_blocks(tmp_path, free):
    with pytest.raises(DiskError):
        check_directory(tmp_path, query=lambda p: free)


def test_query_and_write_failures_block(tmp_path):
    def failed(_):
        raise PermissionError("denied")
    with pytest.raises(DiskError):
        check_directory(tmp_path, query=failed)
    with pytest.raises(DiskError):
        check_directory(tmp_path, query=lambda _: MIN_VIDEO_BYTES, probe=failed)


def test_actual_path_is_queried_and_backup_budget_is_separate(tmp_path):
    output = tmp_path / "videos"
    backup = tmp_path / "backups"
    output.mkdir()
    backup.mkdir()
    counts = {output: MIN_VIDEO_BYTES, backup: 1024}
    assert check_directory(output, query=counts.__getitem__).required_bytes == MIN_VIDEO_BYTES
    assert check_directory(backup, 1024, query=counts.__getitem__).available_bytes == 1024
    with pytest.raises(DiskError):
        check_directory(backup, 1025, query=counts.__getitem__)
    assert list(output.iterdir()) == list(backup.iterdir()) == []


def test_missing_or_file_directory_block(tmp_path):
    with pytest.raises(DiskError):
        check_directory(tmp_path / "absent")
    file = tmp_path / "file"
    file.write_text("user")
    with pytest.raises(DiskError):
        check_directory(file)
    assert file.read_text() == "user"
