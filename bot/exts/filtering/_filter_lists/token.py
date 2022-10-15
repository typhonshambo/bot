from __future__ import annotations

import re
import typing
from functools import reduce
from operator import or_

from bot.exts.filtering._filter_context import Event, FilterContext
from bot.exts.filtering._filter_lists.filter_list import FilterList, ListType
from bot.exts.filtering._filters.filter import Filter
from bot.exts.filtering._filters.token import TokenFilter
from bot.exts.filtering._settings import ActionSettings
from bot.exts.filtering._utils import clean_input

if typing.TYPE_CHECKING:
    from bot.exts.filtering.filtering import Filtering

SPOILER_RE = re.compile(r"(\|\|.+?\|\|)", re.DOTALL)


class TokensList(FilterList):
    """
    A list of filters, each looking for a specific token in the given content given as regex.

    The blacklist defaults dictate what happens by default when a filter is matched, and can be overridden by
    individual filters.

    Usually, if blocking literal strings, the literals themselves can be specified as the filter's value.
    But since this is a list of regex patterns, be careful of the items added. For example, a dot needs to be escaped
    to function as a literal dot.
    """

    name = "token"

    def __init__(self, filtering_cog: Filtering):
        super().__init__()
        filtering_cog.subscribe(self, Event.MESSAGE, Event.MESSAGE_EDIT)

    def get_filter_type(self, content: str) -> type[Filter]:
        """Get a subclass of filter matching the filter list and the filter's content."""
        return TokenFilter

    @property
    def filter_types(self) -> set[type[Filter]]:
        """Return the types of filters used by this list."""
        return {TokenFilter}

    async def actions_for(self, ctx: FilterContext) -> tuple[ActionSettings | None, list[str]]:
        """Dispatch the given event to the list's filters, and return actions to take and messages to relay to mods."""
        text = ctx.content
        if not text:
            return None, []
        if SPOILER_RE.search(text):
            text = self._expand_spoilers(text)
        text = clean_input(text)
        ctx = ctx.replace(content=text)

        triggers = self[ListType.DENY].filter_list_result(ctx)
        actions = None
        messages = []
        if triggers:
            action_defaults = self[ListType.DENY].defaults.actions
            actions = reduce(
                or_,
                (filter_.actions.fallback_to(action_defaults) if filter_.actions else action_defaults
                 for filter_ in triggers
                 )
            )
            if len(triggers) == 1:
                message = f"#{triggers[0].id} (`{triggers[0].content}`)"
                if triggers[0].description:
                    message += f" - {triggers[0].description}"
                messages = [message]
            else:
                messages = [f"#{filter_.id} (`{filter_.content}`)" for filter_ in triggers]
        return actions, messages

    @staticmethod
    def _expand_spoilers(text: str) -> str:
        """Return a string containing all interpretations of a spoilered message."""
        split_text = SPOILER_RE.split(text)
        return ''.join(
            split_text[0::2] + split_text[1::2] + split_text
        )
