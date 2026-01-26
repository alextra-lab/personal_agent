"""Tests for tool implementations."""

from pathlib import Path
from tempfile import TemporaryDirectory

from personal_agent.tools.filesystem import list_directory_executor, read_file_executor
from personal_agent.tools.system_health import system_metrics_snapshot_executor


def test_read_file_success() -> None:
    """Test read_file_executor successfully reads a file."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.txt"
        test_content = "Hello, world!\nThis is a test file."
        test_file.write_text(test_content)

        result = read_file_executor(str(test_file))

        assert result["success"] is True
        assert result["content"] == test_content
        assert result["size_bytes"] == len(test_content.encode("utf-8"))
        assert result["error"] is None


def test_read_file_nonexistent() -> None:
    """Test read_file_executor handles nonexistent file."""
    result = read_file_executor("/nonexistent/file.txt")

    assert result["success"] is False
    assert result["content"] is None
    assert "not found" in result["error"].lower()


def test_read_file_is_directory() -> None:
    """Test read_file_executor handles directory path."""
    with TemporaryDirectory() as tmpdir:
        result = read_file_executor(tmpdir)

        assert result["success"] is False
        assert "not a file" in result["error"].lower()


def test_read_file_size_limit() -> None:
    """Test read_file_executor enforces size limit."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "large.txt"
        # Create a file larger than default 10MB limit
        large_content = "x" * (11 * 1024 * 1024)  # 11 MB
        test_file.write_text(large_content)

        result = read_file_executor(str(test_file), max_size_mb=10)

        assert result["success"] is False
        assert "exceeds limit" in result["error"].lower()
        assert result["size_bytes"] == len(large_content.encode("utf-8"))


def test_read_file_within_size_limit() -> None:
    """Test read_file_executor allows files within size limit."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "small.txt"
        test_content = "Small file content"
        test_file.write_text(test_content)

        result = read_file_executor(str(test_file), max_size_mb=10)

        assert result["success"] is True
        assert result["content"] == test_content


def test_read_file_permission_error() -> None:
    """Test read_file_executor handles permission errors gracefully."""
    # On Unix systems, try to read a protected directory entry
    # This may not work on all systems, so we'll just test the error handling
    result = read_file_executor("/root/.ssh/id_rsa")  # Usually protected

    # Should return an error (either permission denied or not found)
    assert result["success"] is False
    assert result["error"] is not None


def test_list_directory_success() -> None:
    """Test list_directory_executor successfully lists directory contents."""
    with TemporaryDirectory() as tmpdir:
        # Create some test files and directories
        test_dir = Path(tmpdir)
        (test_dir / "file1.txt").write_text("content1")
        (test_dir / "file2.txt").write_text("content2")
        (test_dir / "subdir").mkdir()

        result = list_directory_executor(str(test_dir))

        assert result["success"] is True
        assert result["entries"] is not None
        assert isinstance(result["entries"], list)
        assert result["entry_count"] == 3
        assert result["error"] is None

        # Verify entry structure
        entry_names = [e["name"] for e in result["entries"]]
        assert "file1.txt" in entry_names
        assert "file2.txt" in entry_names
        assert "subdir" in entry_names

        # Verify entry types
        for entry in result["entries"]:
            assert "name" in entry
            assert "type" in entry
            assert "path" in entry
            assert entry["type"] in ["file", "directory"]
            if entry["type"] == "file":
                assert "size_bytes" in entry


def test_list_directory_nonexistent() -> None:
    """Test list_directory_executor handles nonexistent directory."""
    result = list_directory_executor("/nonexistent/directory")

    assert result["success"] is False
    assert result["entries"] is None
    assert "not found" in result["error"].lower()


def test_list_directory_is_file() -> None:
    """Test list_directory_executor handles file path."""
    with TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.txt"
        test_file.write_text("content")

        result = list_directory_executor(str(test_file))

        assert result["success"] is False
        assert "not a directory" in result["error"].lower()


def test_list_directory_empty() -> None:
    """Test list_directory_executor handles empty directory."""
    with TemporaryDirectory() as tmpdir:
        result = list_directory_executor(tmpdir)

        assert result["success"] is True
        assert result["entries"] == []
        assert result["entry_count"] == 0
        assert result["error"] is None


def test_list_directory_with_home_expansion() -> None:
    """Test list_directory_executor expands ~ in path."""
    import os

    home_dir = os.path.expanduser("~")
    if Path(home_dir).exists() and Path(home_dir).is_dir():
        result = list_directory_executor("~")

        assert result["success"] is True
        assert result["entries"] is not None
        assert isinstance(result["entries"], list)


def test_list_directory_with_home_env_var() -> None:
    """Test list_directory_executor expands $HOME environment variable."""
    import os

    home_dir = os.environ.get("HOME", os.path.expanduser("~"))
    if Path(home_dir).exists() and Path(home_dir).is_dir():
        # Test with $HOME
        result = list_directory_executor("$HOME")

        assert result["success"] is True
        assert result["entries"] is not None
        assert isinstance(result["entries"], list)

        # Test with $HOME/Dev if it exists
        dev_path = os.path.join(home_dir, "Dev")
        if Path(dev_path).exists() and Path(dev_path).is_dir():
            result2 = list_directory_executor("$HOME/Dev")
            assert result2["success"] is True
            assert result2["entries"] is not None


def test_system_metrics_snapshot_success() -> None:
    """Test system_metrics_snapshot_executor returns metrics."""
    result = system_metrics_snapshot_executor()

    assert result["success"] is True
    assert result["metrics"] is not None
    assert isinstance(result["metrics"], dict)
    assert result["error"] is None

    # Check for expected metric keys (from sensors)
    metrics = result["metrics"]
    # Should have at least CPU and memory metrics
    assert "perf_system_cpu_load" in metrics or "perf_system_mem_used" in metrics


def test_system_metrics_snapshot_structure() -> None:
    """Test system_metrics_snapshot returns properly structured data."""
    result = system_metrics_snapshot_executor()

    assert result["success"] is True
    metrics = result["metrics"]

    # Metrics should be a dictionary with numeric values (or tuples for some GPU metrics)
    for key, value in metrics.items():
        assert isinstance(key, str)
        # Values should be numeric (int or float), tuple, or None
        assert isinstance(value, (int, float, tuple)) or value is None
