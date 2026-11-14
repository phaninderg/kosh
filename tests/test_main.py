"""The command line is only a launcher: everything else is done in the browser."""

import importlib.util

import pytest

import main


def test_no_command_or_help_explains_how_to_start(capsys):
    assert main.main([]) == 0 and "python main.py ui" in capsys.readouterr().out
    assert main.main(["--help"]) == 0 and "browser" in capsys.readouterr().out


@pytest.mark.parametrize("gone", ["import-cas", "import-statement", "sync", "review", "set-password",
                                  "full", "data", "advise", "withdraw", "recommend", "chat"])
def test_there_are_no_data_or_password_commands(gone, capsys):
    assert main.main([gone]) == 1
    assert "python main.py ui" in capsys.readouterr().out


def test_the_modules_behind_the_removed_commands_are_gone():
    for gone in ("finance.cli", "finance.actions"):
        assert importlib.util.find_spec(gone) is None, gone


def test_the_app_no_longer_needs_the_analysis_stack():
    for gone in ("agent", "tools", "openai", "pandas", "yfinance", "tabulate"):
        assert importlib.util.find_spec(gone) is None, gone
