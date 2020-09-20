from __future__ import annotations

import logging
import re
import string
import textwrap
from functools import partial
from typing import Callable, Iterable, List, Optional, TYPE_CHECKING, Tuple, Union

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag

from .html import Strainer
from .markdown import DocMarkdownConverter
if TYPE_CHECKING:
    from .cog import DocItem

log = logging.getLogger(__name__)

_UNWANTED_SIGNATURE_SYMBOLS_RE = re.compile(r"\[source]|\\\\|¶")
_WHITESPACE_AFTER_NEWLINES_RE = re.compile(r"(?<=\n\n)(\s+)")

_SEARCH_END_TAG_ATTRS = (
    "data",
    "function",
    "class",
    "exception",
    "seealso",
    "section",
    "rubric",
    "sphinxsidebar",
)

_NO_SIGNATURE_GROUPS = {
    "attribute",
    "envvar",
    "setting",
    "tempaltefilter",
    "templatetag",
    "term",
}
_MAX_DESCRIPTION_LENGTH = 1800
_TRUNCATE_STRIP_CHARACTERS = "!?:;." + string.whitespace


def _find_elements_until_tag(
        start_element: PageElement,
        tag_filter: Union[Tuple[str, ...], Callable[[Tag], bool]],
        *,
        func: Callable,
        include_strings: bool = False,
        limit: int = None,
) -> List[Union[Tag, NavigableString]]:
    """
    Get all elements up to `limit` or until a tag matching `tag_filter` is found.

    `tag_filter` can be either a tuple of string names to check against,
    or a filtering callable that's applied to tags.

    When `include_strings` is True, `NavigableString`s from the document will be included in the result along `Tag`s.

    `func` takes in a BeautifulSoup unbound method for finding multiple elements, such as `BeautifulSoup.find_all`.
    The method is then iterated over and all elements until the matching tag or the limit are added to the return list.
    """
    use_tuple_filter = isinstance(tag_filter, tuple)
    elements = []

    for element in func(start_element, name=Strainer(include_strings=include_strings), limit=limit):
        if isinstance(element, Tag):
            if use_tuple_filter:
                if element.name in tag_filter:
                    break
            elif tag_filter(element):
                break
        elements.append(element)

    return elements


_find_next_children_until_tag = partial(_find_elements_until_tag, func=partial(BeautifulSoup.find_all, recursive=False))
_find_next_siblings_until_tag = partial(_find_elements_until_tag, func=BeautifulSoup.find_next_siblings)
_find_previous_siblings_until_tag = partial(_find_elements_until_tag, func=BeautifulSoup.find_previous_siblings)


def _get_general_description(start_element: PageElement) -> Iterable[Union[Tag, NavigableString]]:
    """
    Get page content to a table or a tag with its class in `SEARCH_END_TAG_ATTRS`.

    A headerlink a tag is attempted to be found to skip repeating the symbol information in the description,
    if it's found it's used as the tag to start the search from instead of the `start_element`.
    """
    header = start_element.find_next("a", attrs={"class": "headerlink"})
    start_tag = header.parent if header is not None else start_element
    return _find_next_siblings_until_tag(start_tag, _match_end_tag, include_strings=True)


def _get_dd_description(symbol: PageElement) -> List[Union[Tag, NavigableString]]:
    """Get the contents of the next dd tag, up to a dt or a dl tag."""
    description_tag = symbol.find_next("dd")
    return _find_next_children_until_tag(description_tag, ("dt", "dl"), include_strings=True)


def _get_signatures(start_signature: PageElement) -> List[str]:
    """
    Collect up to 3 signatures from dt tags around the `start_signature` dt tag.

    First the signatures under the `start_signature` are included;
    if less than 2 are found, tags above the start signature are added to the result if any are present.
    """
    signatures = []
    for element in (
            *reversed(_find_previous_siblings_until_tag(start_signature, ("dd",), limit=2)),
            start_signature,
            *_find_next_siblings_until_tag(start_signature, ("dd",), limit=2),
    )[-3:]:
        signature = _UNWANTED_SIGNATURE_SYMBOLS_RE.sub("", element.text)

        if signature:
            signatures.append(signature)

    return signatures


def _get_truncated_description(
        elements: Iterable[Union[Tag, NavigableString]],
        markdown_converter: DocMarkdownConverter,
        max_length: int,
) -> str:
    """
    Truncate markdown from `elements` to be at most `max_length` characters visually.

    `max_length` limits the length of the rendered characters in the string,
    with the real string length limited to `_MAX_DESCRIPTION_LENGTH` to accommodate discord length limits
    """
    visual_length = 0
    real_length = 0
    result = []
    shortened = False

    for element in elements:
        is_tag = isinstance(element, Tag)
        element_length = len(element.text) if is_tag else len(element)
        if visual_length + element_length < max_length:
            if is_tag:
                element_markdown = markdown_converter.process_tag(element)
            else:
                element_markdown = markdown_converter.process_text(element)

            element_markdown_length = len(element_markdown)
            if real_length + element_markdown_length < _MAX_DESCRIPTION_LENGTH:
                result.append(element_markdown)
            else:
                shortened = True
                break
            real_length += element_markdown_length
            visual_length += element_length
        else:
            shortened = True
            break

    markdown_string = "".join(result)
    if shortened:
        markdown_string = markdown_string.rstrip(_TRUNCATE_STRIP_CHARACTERS) + "..."
    return markdown_string


def _parse_into_markdown(signatures: Optional[List[str]], description: Iterable[Tag], url: str) -> str:
    """
    Create a markdown string with the signatures at the top, and the converted html description below them.

    The signatures are wrapped in python codeblocks, separated from the description by a newline.
    The result string is truncated to be max 1000 symbols long.
    """
    description = _get_truncated_description(description, DocMarkdownConverter(bullets="•", page_url=url), 750)
    description = _WHITESPACE_AFTER_NEWLINES_RE.sub('', description)
    if signatures is not None:
        formatted_markdown = "".join(f"```py\n{textwrap.shorten(signature, 500)}```" for signature in signatures)
    else:
        formatted_markdown = ""
    formatted_markdown += f"\n{description}"

    return formatted_markdown


def _match_end_tag(tag: Tag) -> bool:
    """Matches `tag` if its class value is in `SEARCH_END_TAG_ATTRS` or the tag is table."""
    for attr in _SEARCH_END_TAG_ATTRS:
        if attr in tag.get("class", ()):
            return True

    return tag.name == "table"


def get_symbol_markdown(soup: BeautifulSoup, symbol_data: DocItem) -> str:
    """
    Return parsed markdown of the passed symbol using the passed in soup, truncated to 1000 characters.

    The method of parsing and what information gets included depends on the symbol's group.
    """
    symbol_heading = soup.find(id=symbol_data.symbol_id)
    if symbol_heading is None:
        log.warning("Symbol present in loaded inventories not found on site, consider refreshing inventories.")
        return "Unable to parse the requested symbol."
    signature = None
    # Modules, doc pages and labels don't point to description list tags but to tags like divs,
    # no special parsing can be done so we only try to include what's under them.
    if symbol_data.group in {"module", "doc", "label"}:
        description = _get_general_description(symbol_heading)

    elif symbol_heading.name != "dt":
        # Use the general parsing for symbols that aren't modules, docs or labels and aren't dt tags,
        # log info the tag can be looked at.
        description = _get_general_description(symbol_heading)

    elif symbol_data.group in _NO_SIGNATURE_GROUPS:
        description = _get_dd_description(symbol_heading)

    else:
        signature = _get_signatures(symbol_heading)
        description = _get_dd_description(symbol_heading)
    return _parse_into_markdown(signature, description, symbol_data.url).replace('¶', '')
