import json

from projectbrain import cli


def test_index_command_prints_json_stats(tmp_project, capsys):
    rc = cli.main(["--path", str(tmp_project), "index"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["files"] == 8


def test_query_command_prints_json_by_default(tmp_project, capsys):
    cli.main(["--path", str(tmp_project), "index"])
    capsys.readouterr()  # discard index output

    rc = cli.main(["--path", str(tmp_project), "query", "discount"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert "results" in out
    assert "subgraph" in out
    assert "entrypoints" in out


def test_context_command_prints_json(tmp_project, capsys):
    cli.main(["--path", str(tmp_project), "index"])
    capsys.readouterr()

    target = str(tmp_project / "services" / "util.py")
    rc = cli.main(["--path", str(tmp_project), "context", target])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["rel_path"] == "services/util.py"


def test_query_json_and_default_are_identical(tmp_project, capsys):
    cli.main(["--path", str(tmp_project), "index"])
    capsys.readouterr()

    cli.main(["--path", str(tmp_project), "query", "discount", "--json"])
    json_out = capsys.readouterr().out

    cli.main(["--path", str(tmp_project), "query", "discount"])
    default_out = capsys.readouterr().out

    assert json_out == default_out


def test_query_summary_is_human_readable_not_json(tmp_project, capsys):
    cli.main(["--path", str(tmp_project), "index"])
    capsys.readouterr()

    rc = cli.main(["--path", str(tmp_project), "query", "discount", "--summary"])
    assert rc == 0

    out = capsys.readouterr().out
    assert out.startswith("Top files:")
    assert "services/discount.py" in out
    assert "Reason:" in out
    try:
        json.loads(out)
        assert False, "summary output should not be valid JSON"
    except json.JSONDecodeError:
        pass


def test_context_summary_is_human_readable(tmp_project, capsys):
    cli.main(["--path", str(tmp_project), "index"])
    capsys.readouterr()

    target = str(tmp_project / "services" / "util.py")
    rc = cli.main(["--path", str(tmp_project), "context", target, "--summary"])
    assert rc == 0

    out = capsys.readouterr().out
    assert out.startswith("services/util.py")
    assert "Functions: helper" in out
    assert "Imported by:" in out
