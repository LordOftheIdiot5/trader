"""The research seat: the only one that can see outside the price series.

This exists because of a specific observation. Given nothing but forty price
points, the desk correctly and repeatedly concluded there was nothing to
trade - not out of caution, but because that is the only conclusion available
from that data. A desk that can only see prices can only ever say the prices
are flat.

Deliberately a separate, unstructured call rather than a tool on one of the
existing seats:

  - Server-side tools and structured output interact in ways that are easy to
    get subtly wrong, and a malformed research call must not take a working
    decision down with it.
  - The result is prose that every other seat reads. One search, four readers.
  - It degrades to nothing. If the search fails, is rate limited, or returns
    junk, the desk runs exactly as it did before with an empty brief. Research
    is an input, not a dependency.

A caution that belongs next to the code rather than in a README: adding news
does not add edge. A model reading headlines is not a model that knows what
happens next, and a confident narrative built on a fresh article is still a
narrative. What this buys is that the desk is reasoning about the world rather
than about forty numbers - which is a precondition for a useful opinion, not a
substitute for one.
"""

from __future__ import annotations

# The modern variant carries dynamic filtering and needs Opus 4.6+ / Sonnet
# 4.6+. Everything older, Haiku 4.5 included, takes the basic one. Sending the
# wrong variant is a hard error rather than a downgrade.
MODERN_SEARCH = "web_search_20260209"
BASIC_SEARCH = "web_search_20250305"

# Each search costs tokens on the way back in. Three is enough for a handful
# of symbols and bounded enough that a chatty run cannot run up a bill.
MAX_SEARCHES = 3

RESEARCH_BRIEF = """
You are the research seat on a small trading desk. You are the only seat that
can see anything beyond the price series, so the rest of the desk depends on
you for context it otherwise does not have.

Search for recent, material news about the assets named below. Material means
something that plausibly changes what an asset is worth: a protocol upgrade,
an exchange listing or delisting, a regulatory decision, a hack, a large
liquidation, an ETF flow, a macro print that moves risk assets broadly.

What to report, in plain prose, under 300 words total:

  - Only what you actually found. If a search returns nothing material about
    an asset, say "nothing material found" for it. Do not fill the space.
  - The date of anything you cite. A three-week-old story is background, not
    news, and the desk needs to be able to tell the difference.
  - Where the market already knows. If something is widely reported it is
    probably in the price, and saying so is more useful than repeating it.

What not to do:

  - Do not give a view on whether to buy or sell. That is not your seat. Report
    what happened; the analysts and the chair decide what it means.
  - Do not speculate beyond your sources, and never present a guess as a
    finding. "Nothing material found" is a good answer and is often the true
    one.
  - Do not reason from anything you remember rather than found. Your training
    data is old and this desk is trading now.
""".strip()


def search_tool(model: str) -> dict:
    from .desk import supports_adaptive_thinking

    variant = MODERN_SEARCH if supports_adaptive_thinking(model) else BASIC_SEARCH
    return {"type": variant, "name": "web_search", "max_uses": MAX_SEARCHES}


def gather(client, model: str, symbols) -> str:
    """Return a research brief, or an empty string if anything goes wrong.

    Never raises. The desk has run without research since it was built and
    must keep being able to.
    """
    assets = ", ".join(symbols)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=RESEARCH_BRIEF,
            tools=[search_tool(model)],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Assets: {assets}\n\n"
                        "Search for material news from the last few days and "
                        "report what you find."
                    ),
                }
            ],
        )
    except Exception as error:
        print(f"research seat unavailable: {error}")
        return ""

    if getattr(response, "stop_reason", None) == "refusal":
        return ""

    # Server tool failures arrive as a 200 with an error object in the result
    # block rather than as an exception, so the text blocks are what matter -
    # if the search failed, the model simply has less to say.
    parts = [
        block.text
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text" and getattr(block, "text", "").strip()
    ]
    return "\n\n".join(parts).strip()
