from mcp_1c import release_helper


def test_default_release_notes_include_tag_and_verification(monkeypatch):
    def fake_git_output(args):
        if args[:2] == ["describe", "--tags"]:
            raise release_helper.subprocess.CalledProcessError(1, ["git", *args])
        return "- Fix one (abc123)"

    monkeypatch.setattr(release_helper, "_git_output", fake_git_output)

    notes = release_helper._default_release_notes("v1.2.3")

    assert "# Answer42 v1.2.3" in notes
    assert "- Fix one (abc123)" in notes
    assert "## Verification" in notes


def test_ensure_release_notes_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(release_helper, "_default_release_notes", lambda tag: f"notes for {tag}\n")

    notes_path = release_helper._ensure_release_notes(tmp_path, "v1.2.3", dry_run=False, overwrite=False)

    assert notes_path == tmp_path / "release-notes" / "v1.2.3.md"
    assert notes_path.read_text(encoding="utf-8") == "notes for v1.2.3\n"


def test_existing_release_notes_are_preserved(tmp_path, monkeypatch):
    notes_path = tmp_path / "release-notes" / "v1.2.3.md"
    notes_path.parent.mkdir()
    notes_path.write_text("custom\n", encoding="utf-8")
    monkeypatch.setattr(release_helper, "_default_release_notes", lambda tag: "generated\n")

    assert release_helper._ensure_release_notes(tmp_path, "v1.2.3", dry_run=False, overwrite=False) == notes_path
    assert notes_path.read_text(encoding="utf-8") == "custom\n"
