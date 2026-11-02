import pytest

import generate_yaml


@pytest.fixture(autouse=True)
def plain_questionary_print(monkeypatch):
    """Route questionary.print through print() so capsys sees what the user would.

    prompt_toolkit otherwise keeps the stdout of the first test that printed,
    which pytest has closed by the time later tests print.
    """
    monkeypatch.setattr(
        generate_yaml.questionary, "print", lambda text, **_kwargs: print(text)
    )


class PromptScript:
    """Replays scripted answers to questionary confirm/text prompts."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.confirm_answers: list[bool] = []  # exhausted -> yes
        self.text_answers: list[str] = []
        self.password_answers: list[str] = []

    def _fake(self, answers: list, *, exhausted):
        script = self

        class FakePrompt:
            def __init__(self, message, **_kwargs):
                script.asked.append(message)

            def ask(self):
                return answers.pop(0) if answers else exhausted()

        return FakePrompt


def _no_text_answer():
    raise AssertionError("unexpected text prompt")


@pytest.fixture
def prompts(monkeypatch):
    script = PromptScript()
    monkeypatch.setattr(
        generate_yaml.questionary,
        "confirm",
        script._fake(script.confirm_answers, exhausted=lambda: True),
    )
    monkeypatch.setattr(
        generate_yaml.questionary,
        "text",
        script._fake(script.text_answers, exhausted=_no_text_answer),
    )
    monkeypatch.setattr(
        generate_yaml.questionary,
        "password",
        script._fake(script.password_answers, exhausted=_no_text_answer),
    )
    return script
