def test_tools_dir_on_path():
    import sys

    assert any(p.replace("\\", "/").endswith("tools") for p in sys.path)
