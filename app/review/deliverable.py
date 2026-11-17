"""Phase 8 settings (summary, export, rewrite chat), read once from `configs/app.yaml`.

One frozen object the service carries, so the three Wave 1 slices read their knobs from
the same place and tests build it directly instead of patching a config file. Defaults
match the shipped config: in particular the LLM recommendation is off unless
`summary.llm_enabled` says otherwise (Phase 7-10 plan, "Decisions taken" #4).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.paths import CONFIGS, ROOT

DEFAULT_TEXT_MODEL = {"provider": "gemini", "name": "gemini-3.8-flash"}


@dataclass(frozen=True)
class SummarySettings:
    llm_enabled: bool = False
    top_risks: int = 5
    excerpt_chars: int = 240
    # `models.summary`: provider, name, max_output_tokens (core.llm.complete's model dict).
    model: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_TEXT_MODEL))


@dataclass(frozen=True)
class ExportSettings:
    # The DOCX template and its assets (configs/export/README.md describes them).
    template_dir: Path = CONFIGS / "export"
    # Statute texts of every cited § in an appendix of the memo.
    statute_appendix: bool = True


@dataclass(frozen=True)
class ChatSettings:
    max_output_tokens: int = 800
    max_answer_chars: int = 4000
    max_history_messages: int = 10
    # `models.chat`: provider, name (streamed generation).
    model: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_TEXT_MODEL))


@dataclass(frozen=True)
class DeliverableSettings:
    summary: SummarySettings = field(default_factory=SummarySettings)
    export: ExportSettings = field(default_factory=ExportSettings)
    chat: ChatSettings = field(default_factory=ChatSettings)


def _model(config: dict[str, Any], name: str) -> dict[str, Any]:
    models = config.get("models") or {}
    return dict(models.get(name) or models.get("explain") or DEFAULT_TEXT_MODEL)


def deliverable_settings(config: dict[str, Any] | None = None) -> DeliverableSettings:
    """The `summary`, `export` and `chat` blocks; a missing key keeps its default.
    `models.summary` and `models.chat` fall back to `models.explain`."""
    if config is None:
        from core.config import load_yaml

        config = load_yaml(CONFIGS / "app.yaml")
    summary = config.get("summary") or {}
    export = config.get("export") or {}
    chat = config.get("chat") or {}
    template_dir = Path(export.get("template_dir", "configs/export"))
    if not template_dir.is_absolute():
        template_dir = ROOT / template_dir
    return DeliverableSettings(
        summary=SummarySettings(
            llm_enabled=bool(summary.get("llm_enabled", False)),
            top_risks=int(summary.get("top_risks", 5)),
            excerpt_chars=int(summary.get("excerpt_chars", 240)),
            model=_model(config, "summary"),
        ),
        export=ExportSettings(
            template_dir=template_dir,
            statute_appendix=bool(export.get("statute_appendix", True)),
        ),
        chat=ChatSettings(
            max_output_tokens=int(chat.get("max_output_tokens", 800)),
            max_answer_chars=int(chat.get("max_answer_chars", 4000)),
            max_history_messages=int(chat.get("max_history_messages", 10)),
            model=_model(config, "chat"),
        ),
    )
