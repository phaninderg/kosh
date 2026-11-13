"""The command line: what it offers, and that the pieces that were removed stay removed."""

import pytest

import main


def test_no_command_or_help_prints_the_commands(capsys):
    assert main.main([]) == 0 and "python main.py ui" in capsys.readouterr().out
    assert main.main(["--help"]) == 0 and "import-statement" in capsys.readouterr().out


@pytest.mark.parametrize("gone", ["full", "data", "advise", "withdraw", "recommend", "chat"])
def test_commands_from_the_analysis_pipeline_are_not_available(gone, capsys):
    assert main.main([gone]) == 1
    assert "python main.py ui" in capsys.readouterr().out


def test_import_commands_need_a_file(capsys):
    assert main.main(["import-cas"]) == 1 and "Usage" in capsys.readouterr().out
    assert main.main(["import-statement", "--parser=icici"]) == 1 and "Usage" in capsys.readouterr().out


def test_the_app_no_longer_needs_the_analysis_stack():
    import importlib.util
    for gone in ("agent", "tools", "openai", "pandas", "yfinance", "tabulate"):
        assert importlib.util.find_spec(gone) is None, gone
