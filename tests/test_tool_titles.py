"""DefaultToolTitles middleware: derive a human-readable title per tool."""

from __future__ import annotations

from fastmcp.tools.tool import Tool

from mcp_data_core.mcp.middleware import DefaultToolTitles, _humanize_tool_name


def test_humanize_basic() -> None:
    assert _humanize_tool_name("get_drug_label") == "Get Drug Label"
    assert _humanize_tool_name("search_clinical_trials") == "Search Clinical Trials"


def test_humanize_keeps_acronyms_upper() -> None:
    assert _humanize_tool_name("search_fda_orange_book_products") == "Search FDA Orange Book Products"
    assert _humanize_tool_name("query_cms_physician_fee_schedule") == "Query CMS Physician Fee Schedule"
    assert _humanize_tool_name("search_nppes_providers") == "Search NPPES Providers"


async def test_fills_missing_title_and_preserves_explicit() -> None:
    def get_drug_label() -> None: ...

    def my_tool() -> None: ...

    missing = Tool.from_function(get_drug_label)
    explicit = Tool.from_function(my_tool).model_copy(update={"title": "My Custom Title"})

    async def call_next(_ctx: object) -> list[Tool]:
        return [missing, explicit]

    out = await DefaultToolTitles().on_list_tools(None, call_next)
    titles = {t.name: t.title for t in out}

    assert titles["get_drug_label"] == "Get Drug Label"  # derived
    assert titles["my_tool"] == "My Custom Title"  # untouched


async def test_title_serializes_to_wire() -> None:
    def get_drug_recall() -> None: ...

    async def call_next(_ctx: object) -> list[Tool]:
        return [Tool.from_function(get_drug_recall)]

    out = await DefaultToolTitles().on_list_tools(None, call_next)
    assert out[0].to_mcp_tool().title == "Get Drug Recall"
