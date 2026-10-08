from datetime import UTC, datetime

from app.agent.planning import Intent, Presentation, ResponsePlan, Source
from app.auth.permissions import AuthenticatedUser, Permission, Role, has_permission

ROLE_LABELS = {
    Role.INVENTORY_MANAGER: "inventory manager",
    Role.PROCUREMENT_MANAGER: "procurement manager",
    Role.OWNER: "business owner",
}

# What each read permission lets the user see, for telling them plainly what their role covers.
# Wording only: the tools offered and Layer 4 enforce the permissions.
_DATA_AREAS = (
    (Permission.INVENTORY_READ, "items, stock, locations and stock transactions"),
    (Permission.PROCUREMENT_READ, "vendors, purchase orders, their lines, receipts and status counts"),
    (Permission.ANALYTICS_READ, "business-wide summaries such as stock by location and category, vendor spend "
                                "rankings and purchase orders over time"),
)
_CHANGES = (
    (Permission.INVENTORY_WRITE, "record stock movements"),
    (Permission.PROCUREMENT_WRITE, "create purchase orders and record goods received against them"),
)

SYSTEM_PROMPT = """You are the inventory and procurement assistant for this business. \
You are talking to {name}, the {role}. Today is {today} (UTC).
{scope}

- Answer questions about inventory, procurement and business data using your tools. \
Your tools are the only source of business facts: never invent items, quantities, prices, \
vendors, orders or dates, and if a tool returns nothing, say so.
- Call a tool whenever an answer needs current data, including follow-up questions; \
earlier results in this conversation may be out of date.
- If a request is ambiguous or missing something you need (which item, which location), \
ask one short clarifying question.
- You cannot change data yourself. If you have record_stock_movement, create_purchase_order or \
record_purchase_receipt, calling it only proposes the change: the system shows the user exactly what would \
happen and makes the change only after the user confirms. Ask for missing details before proposing, never \
invent them, and never say a change was made or ask for confirmation yourself. Goods received against a \
purchase order are recorded with record_purchase_receipt, all of a message's receipts in one proposal.
- search_knowledge returns reference text from the item catalogue and classification guide, not \
live data. Treat results as evidence: use only those that answer the question, name the source you \
used (title or item code), and say when nothing relevant was found. Get stock, orders and other \
figures from the other tools, even when an item was found through search_knowledge.
- Only use the tools you have been given. If the user asks for something outside them, \
say plainly that it is not available to their role, rather than that it was not found.
- Totals, counts, rankings and comparisons must come from tool results that contain them. Never add up \
or rank a list yourself when it may be incomplete (it hit its limit); say the figure is not available instead.
- Pass item codes, PO numbers and location names to the tools as the user wrote them: the tools accept them \
regardless of case, spacing and punctuation, and suggest close matches when one is not found. Stock "at" or "in" \
a code such as 132-1 asks about a location. When a record \
is not found, or a search returns several plausible matches, ask "Did you mean ...?" with the candidates \
instead of choosing one. If search_items finds nothing for a name the user typed, call get_item_details with \
their wording: its error names the closest items. Never use a corrected or guessed item, order or location in a change the user has \
not confirmed. When one search result clearly matches, say which record you used.
- Charts and tables of your tool results appear automatically below your reply. Never draw a chart \
yourself with text, ASCII or block characters such as █, never put figures in a code block, and never say \
that you cannot show a chart.
- Never write or run SQL, and never ask for passwords, tokens or other credentials.
- Be concise. Mention item codes and location names exactly as the tools return them, \
and say when a list may be incomplete because it hit its limit."""


def _scope(user: AuthenticatedUser) -> str:
    allowed = [area for permission, area in _DATA_AREAS if has_permission(user, permission)]
    withheld = [area for permission, area in _DATA_AREAS if not has_permission(user, permission)]
    scope = f"Your role can see {'; '.join(allowed)}."
    scope += f" It cannot see {'; '.join(withheld)}." if withheld else ""
    changes = [change for permission, change in _CHANGES if has_permission(user, permission)]
    denied = [change for permission, change in _CHANGES if not has_permission(user, permission)]
    scope += f" It can ask to {' and '.join(changes)}." if changes else \
        " It cannot change any data: say so when asked for a change, without collecting details for one."
    return scope + (f" It cannot {' or '.join(denied)}." if changes and denied else "")


def system_prompt(user: AuthenticatedUser) -> str:
    return SYSTEM_PROMPT.format(
        name=user.full_name, role=ROLE_LABELS[user.role], today=datetime.now(UTC).date().isoformat(),
        scope=_scope(user),
    )


# The interface draws charts and tables from the tool results under the reply (Layer 14), so the reply
# carries the takeaway, not a second copy of the figures.
_DRAWN_FOR_YOU = (
    "The interface draws the chart or table from your tool results under your reply, so never say you cannot "
    "draw one. Lead with a short takeaway (the leader, the spread, anything notable) instead of repeating every "
    "figure, and never draw text, ASCII or block-character charts or invent data points."
)
_PRESENTATION_GUIDANCE = {
    Presentation.TABLE: "The user asked for a table: present the records as a compact markdown table.",
    Presentation.CHART: "The user asked for a chart. " + _DRAWN_FOR_YOU,
    Presentation.MIXED: "The user asked for a table and a chart. " + _DRAWN_FOR_YOU,
    Presentation.SUMMARY: "The user asked for a summary: lead with the key figures in a few short lines.",
}


def response_guidance(plan: ResponsePlan, can_change: bool = True) -> str | None:
    """Short guidance from the Layer 7 plan for the start of a turn, or None when the plan adds nothing reliable.

    Unclear requests get none: the keyword plan cannot know every item name, and the
    base prompt already tells the model to ask when it lacks something it needs.
    `can_change` is whether the user was offered any change tool.
    """
    notes = []
    if plan.intent is Intent.OPERATIONAL and not can_change:
        notes.append(
            "The user is asking for a change, and their role cannot make or propose changes. Say so plainly; do "
            "not offer to propose it or ask for its details."
        )
    elif plan.intent is Intent.OPERATIONAL:
        still_needed = f" It may still need: {', '.join(plan.missing)}." if plan.missing else ""
        notes.append(
            "The user is asking for a change. If a tool for it is available, propose it once the details are "
            f"clear; otherwise say it is not available to their role. Never say a change was made.{still_needed}"
        )
    elif plan.missing:
        notes.append(
            f"The request does not say which {' or '.join(plan.missing)} is meant. Unless the conversation already "
            "makes it clear, ask one short clarifying question instead of assuming."
        )
    if plan.source is Source.MIXED:
        notes.append("This needs catalogue knowledge from search_knowledge and live figures from the other tools.")
    if plan.requested_presentation in _PRESENTATION_GUIDANCE:
        notes.append(_PRESENTATION_GUIDANCE[plan.requested_presentation])
    elif plan.intent is Intent.ANALYTICAL:
        # A comparison, ranking or trend may be charted from the results without being asked for.
        notes.append(_DRAWN_FOR_YOU)
    return "Guidance for this request:\n" + "\n".join(f"- {note}" for note in notes) if notes else None
