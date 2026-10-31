"""HTML for the operator surface, composed in Python.

No template engine, for the reason D108 gave for the Markdown renderer: a template is a
second place numbers get formatted, and formatting is where a figure stops equalling its
source. Composing here means every value arrives as the string the report already
rendered and passes through nothing that could reformat it -- which is what lets the
same copy-never-compose gate run over this output as over the document (D109, D123).

No external asset
-----------------

The stylesheet is inline and there is no script, no font and no CDN. A page that fetched
a stylesheet would render differently on a laptop with no network, and an operator
reviewing a cost estate on a locked-down machine is exactly the person who would
discover that. It also means a screenshot and the page are the same thing.

Escaping
--------

Every interpolated value goes through :func:`esc`. Resource names and tags are
provider-supplied and the injection corpus puts hostile text in precisely those fields,
so HTML escaping is not hygiene here -- it is the same boundary the prompt builder
enforces, in a second output format.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from html import escape

from costsentinel.dashboard.view import (
    AnomalyView,
    EstateView,
    ExemptView,
    FindingView,
    PendingView,
    ReportView,
    Row,
)
from costsentinel.domain.recommendations import ActionClass

#: Inline, deliberately. See the module docstring: no external asset at runtime.
STYLE = """
:root {
  --ink: #10151c; --muted: #5a6572; --line: #dfe4ea; --bg: #f7f8fa;
  --card: #ffffff; --warn: #8a5300; --warnbg: #fff6e5; --stop: #8a1c1c;
  --stopbg: #fdeaea; --ok: #1d5c2f; --okbg: #e9f5ec; --accent: #11456b;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 4rem; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
}
header.top {
  background: var(--accent); color: #fff; padding: 1.1rem 1.5rem;
}
header.top a { color: #fff; }
header.top h1 { margin: 0; font-size: 1.15rem; font-weight: 600; letter-spacing: .01em; }
header.top .sub { opacity: .85; font-size: .85rem; margin-top: .2rem; }
main { max-width: 1040px; margin: 0 auto; padding: 1.5rem; }
.banner {
  background: var(--warnbg); border: 1px solid #f0d9a8; border-left: 4px solid var(--warn);
  color: var(--warn); padding: .8rem 1rem; border-radius: 4px; margin-bottom: 1.25rem;
  font-size: .9rem;
}
section.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 6px;
  padding: 1.1rem 1.25rem; margin-bottom: 1.1rem;
}
section.card > h2 {
  margin: 0 0 .25rem; font-size: 1rem; letter-spacing: .02em; text-transform: uppercase;
  color: var(--muted);
}
section.card > p.lede { margin: .35rem 0 .9rem; color: var(--muted); font-size: .9rem; }
table { width: 100%; border-collapse: collapse; }
table.kv td { padding: .34rem 0; border-bottom: 1px solid var(--line); vertical-align: top; }
table.kv td:first-child { color: var(--muted); width: 58%; }
table.kv td:last-child { text-align: right; font-variant-numeric: tabular-nums; }
table.kv tr.em td { font-weight: 600; color: var(--ink); font-size: 1.05rem; }
table.grid th, table.grid td {
  text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--line);
  font-size: .88rem;
}
table.grid th {
  color: var(--muted); font-weight: 600; font-size: .78rem; text-transform: uppercase;
}
table.grid td.num { text-align: right; font-variant-numeric: tabular-nums; }
.item { border-top: 1px solid var(--line); padding: .9rem 0; }
.item:first-of-type { border-top: 0; }
.item h3 { margin: 0 0 .3rem; font-size: .98rem; }
.item .meta { color: var(--muted); font-size: .84rem; margin-bottom: .45rem; }
.item .why { font-size: .88rem; margin: .35rem 0; }
.tag {
  display: inline-block; padding: .1rem .45rem; border-radius: 3px; font-size: .74rem;
  font-weight: 600; text-transform: uppercase; letter-spacing: .03em;
}
.tag.allow { background: var(--okbg); color: var(--ok); }
.tag.review { background: var(--warnbg); color: var(--warn); }
.tag.block { background: var(--stopbg); color: var(--stop); }
.tag.unknown { background: #eceff3; color: var(--muted); }
.money { font-variant-numeric: tabular-nums; font-weight: 600; }
.money.unknown { font-weight: 400; color: var(--muted); font-style: italic; }
.prose { font-size: .92rem; }
.prose.model { border-left: 3px solid var(--line); padding-left: .8rem; color: #2c3440; }
.caveat { color: var(--muted); font-size: .82rem; margin-top: .5rem; }
ul.plain { margin: .3rem 0 0; padding-left: 1.1rem; font-size: .86rem; color: #2c3440; }
form.decide {
  margin-top: .6rem; display: flex; gap: .4rem; flex-wrap: wrap; align-items: center;
}
form.decide input[type=text] {
  flex: 1 1 18rem; padding: .35rem .5rem; border: 1px solid var(--line); border-radius: 4px;
  font: inherit; font-size: .86rem;
}
form.decide button {
  padding: .35rem .7rem; border: 1px solid var(--line); border-radius: 4px; background: #fff;
  font: inherit; font-size: .84rem; cursor: pointer;
}
form.decide button.primary {
  background: var(--accent); color: #fff; border-color: var(--accent);
}
a.client {
  display: block; padding: .55rem 0; border-bottom: 1px solid var(--line);
  color: var(--accent);
}
footer.foot {
  max-width: 1040px; margin: 0 auto; padding: 0 1.5rem; color: var(--muted);
  font-size: .8rem;
}
"""

#: Said on every page that shows a proposal, because the surface exists to make these
#: visible and the first thing an operator should know is what it will not do.
NO_EXECUTION_NOTE = (
    "Nothing here has been applied. CostSentinel has no execution path: every action "
    "below is a proposal, and approving one hands it to a person to carry out."
)


#: Matches the inline stylesheet, so a figure check can skip it.
_STYLE_BLOCK = re.compile(r"<style>.*?</style>", re.DOTALL)


def content_of(html: str) -> str:
    """The page with its stylesheet removed, for the figure-accuracy check.

    The stylesheet has to come out, and it is worth being precise about why rather than
    hand-waving it. `font: 15px/1.55` and `font-size: 1.05rem` are decimals, and the
    figure checker matches decimals -- it would report a line height as an invented
    monetary figure. A stylesheet is not content: nobody reads a line height as money.

    Only the ``<style>`` element is removed. Tags and attributes are deliberately left
    in, so a figure smuggled into an ``alt``, a ``value`` or a ``title`` is still
    caught; a test puts one in the body and confirms the check still fails. Stripping
    markup as well would have made this function a place for a figure to hide, which is
    the opposite of what it is for.
    """
    return _STYLE_BLOCK.sub("", html)


def esc(value: object) -> str:
    """HTML-escape anything, quotes included.

    ``quote=True`` because values reach attributes as well as text -- a resource name
    carrying a double quote would otherwise end an attribute early, which is the
    classic way a hostile name becomes markup.
    """
    return escape(str(value), quote=True)


def _class_tag(action_class: ActionClass) -> str:
    return f'<span class="tag {esc(action_class.value)}">{esc(action_class.value)}</span>'


def _money(value: str) -> str:
    """A figure, styled as unknown when that is what it says.

    The class is chosen by looking at the *rendered string* rather than by recomputing
    anything: ``MoneyAmount.display()`` already decided whether this is a number or an
    unknown, and reading its answer keeps one decision in one place.
    """
    unknown = "unknown" in value
    return f'<span class="money{" unknown" if unknown else ""}">{esc(value)}</span>'


def _kv(rows: Sequence[Row]) -> str:
    cells = "".join(
        f'<tr class="{"em" if row.emphasis else ""}">'
        f"<td>{esc(row.label)}</td><td>{_money(row.value)}</td></tr>"
        for row in rows
    )
    return f'<table class="kv">{cells}</table>'


def _page(title: str, body: str, *, subtitle: str = "") -> str:
    """One document, with the stylesheet inline and nothing fetched."""
    sub = f'<div class="sub">{esc(subtitle)}</div>' if subtitle else ""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f"<style>{STYLE}</style>"
        "</head><body>"
        f'<header class="top"><h1>{esc(title)}</h1>{sub}</header>'
        f"<main>{body}</main>"
        '<footer class="foot">CostSentinel — read-only. No execution path exists.</footer>'
        "</body></html>"
    )


def _banner(text: str) -> str:
    if not text:
        return ""
    # The banner's own text is Markdown from the report; the emphasis markers are
    # stripped rather than rendered, since this format marks up differently.
    cleaned = text.replace("**", "").removeprefix("> ")
    return f'<div class="banner">{esc(cleaned)}</div>'


def _finding_item(finding: FindingView, *, show_rank: bool) -> str:
    heading = (
        f"{finding.rank}. {finding.resource_name} — {finding.action}"
        if show_rank
        else f"{finding.resource_name} — {finding.action}"
    )
    saving = (
        f"<td>Saving if applied</td><td>{_money(finding.monthly_saving)} / month "
        f"({esc(finding.annual_saving)} / year)</td>"
        if finding.saving_is_known
        else f"<td>Saving</td><td>{_money(finding.monthly_saving)} — see below</td>"
    )
    rows = [
        f"<tr><td>What</td><td>{esc(finding.waste_kind)}</td></tr>",
        f"<tr><td>Subscription</td><td><code>{esc(finding.subscription_id)}</code></td></tr>",
        f"<tr><td>Costs now</td><td>{_money(finding.current_cost)} / month</td></tr>",
        f'<tr class="em">{saving}</tr>',
    ]
    if finding.saving_is_known:
        rows.append(f"<tr><td>Figure</td><td>{esc(finding.figure_basis)}</td></tr>")
    rows.append(f"<tr><td>Approval</td><td>{_class_tag(finding.action_class)}</td></tr>")
    rows.append(f"<tr><td>Detector confidence</td><td>{esc(finding.confidence)}</td></tr>")

    parts = [
        f'<div class="item"><h3>{esc(heading)}</h3>',
        f'<table class="kv">{"".join(rows)}</table>',
        '<p class="why"><strong>'
        + ("How the saving was derived" if finding.saving_is_known else "Why no figure")
        + f".</strong> {esc(finding.basis)}</p>",
    ]
    if finding.root_cause:
        parts.append(
            f'<p class="why"><strong>Why this exists.</strong> {esc(finding.root_cause)}</p>'
        )
    if finding.ranking_rationale:
        parts.append(
            '<p class="why"><strong>Why it sits here.</strong> '
            f"{esc(finding.ranking_rationale)}</p>"
        )
    if finding.memory_note:
        parts.append(
            '<p class="why"><strong>You have seen this before.</strong> '
            f"{esc(finding.memory_note)}</p>"
        )
    if finding.evidence:
        parts.append(_bullets("Evidence", finding.evidence))
    if finding.preconditions:
        parts.append(
            _bullets("Re-checked immediately before anything changes", finding.preconditions)
        )
    parts.append("</div>")
    return "".join(parts)


def _bullets(label: str, items: Iterable[str]) -> str:
    rows = "".join(f"<li>{esc(one)}</li>" for one in items)
    return f'<p class="why"><strong>{esc(label)}.</strong></p><ul class="plain">{rows}</ul>'


def _exempt_item(item: ExemptView) -> str:
    return (
        f'<div class="item"><h3>{esc(item.resource_name)} — {esc(item.exemption_reason)}</h3>'
        f'<table class="kv">'
        f"<tr><td>What we detected</td><td>{esc(item.waste_kind)}</td></tr>"
        f"<tr><td>Subscription</td><td><code>{esc(item.subscription_id)}</code></td></tr>"
        f'<tr class="em"><td>Costs now</td><td>{_money(item.current_cost)} / month</td></tr>'
        f"<tr><td>Exempt because</td><td><code>{esc(item.matched_tag)}</code></td></tr>"
        f"<tr><td>Rule</td><td><code>{esc(item.exemption_rule)}</code></td></tr>"
        f"</table>"
        f'<p class="why">{esc(item.statement)}</p></div>'
    )


def _anomaly_item(anomaly: AnomalyView) -> str:
    parts = [
        f'<div class="item"><h3>{esc(anomaly.scope)} — {esc(anomaly.kind)}</h3>',
        f'<table class="kv">'
        f"<tr><td>Baseline</td><td>{_money(anomaly.baseline)} / day</td></tr>"
        f"<tr><td>Observed</td><td>{_money(anomaly.observed)} / day</td></tr>"
        f'<tr class="em"><td>Difference</td><td>{_money(anomaly.difference)} / day '
        f"({esc(anomaly.direction)})</td></tr>"
        f"<tr><td>Window</td><td>{esc(anomaly.window)}</td></tr>"
        f"<tr><td>How unusual</td><td>{esc(anomaly.deviation_score)} robust standard "
        f"deviations</td></tr>"
        f"<tr><td>Attributed to</td><td>{esc(anomaly.attribution_summary)}</td></tr>"
        f"<tr><td>Detector confidence</td><td>{esc(anomaly.confidence)}</td></tr>"
        f"</table>",
        f'<p class="why">{esc(anomaly.statement)}</p>',
    ]
    if anomaly.attributed:
        rows = "".join(
            f"<tr><td>{esc(part.label)}</td>"
            f'<td class="num">{_money(part.before)} / day</td>'
            f'<td class="num">{_money(part.after)} / day</td>'
            f'<td class="num">{_money(part.difference)} / day ({esc(part.direction)})</td>'
            f'<td class="num">{esc(part.share)}</td></tr>'
            for part in anomaly.attributed
        )
        parts.append(
            '<p class="why"><strong>What changed.</strong></p>'
            '<table class="grid"><thead><tr><th>Meter</th><th>Was</th><th>Now</th>'
            "<th>Difference</th><th>Share of the change</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        parts.append(
            '<p class="why"><strong>What changed.</strong> '
            f"{esc(anomaly.attribution_statement)}</p>"
        )
    if anomaly.note:
        parts.append(f'<p class="prose model">{esc(anomaly.note)}</p>')
    parts.append("</div>")
    return "".join(parts)


#: Said next to the form, so an approver knows what their name will be worth on the record.
SELF_ASSERTED_NOTE = (
    "Your name is recorded as self-asserted and unverified: nothing on this deployment "
    "checks who you are."
)


def _pending_item(item: PendingView, *, decidable: bool, verifies_identity: bool = False) -> str:
    deferred = f" (deferred {item.deferrals}x)" if item.deferrals else ""
    form = ""
    if decidable:
        # A plain HTML form. No JavaScript: the page has to work for somebody who
        # blocks it, and a decision this consequential should not depend on a script.
        form = (
            f'<form class="decide" method="post" action="/dashboard/decide">'
            f'<input type="hidden" name="recommendation_id" value="{esc(item.recommendation_id)}">'
            '<input type="text" name="reason" required '
            'placeholder="Why — recorded with your decision">'
            f'<input type="text" name="approver" required placeholder="Who you are">'
            '<button class="primary" type="submit" name="decision" '
            'value="approved">Approve</button>'
            f'<button type="submit" name="decision" value="rejected">Reject</button>'
            f'<button type="submit" name="decision" value="deferred">Defer</button>'
            f"</form>"
            f'<p class="caveat">Approving records a decision and hands the action to a '
            f"person. CostSentinel will not and cannot perform it.</p>"
        )
        if not verifies_identity:
            form += f'<p class="caveat">{esc(SELF_ASSERTED_NOTE)}</p>'
    return (
        f'<div class="item"><h3>{esc(item.resource_name)} — {esc(item.action)}{esc(deferred)}</h3>'
        f'<div class="meta">{esc(item.client)} · sweep <code>{esc(item.run_id)}</code> · '
        f"<code>{esc(item.recommendation_id)}</code> {_class_tag(item.action_class)}</div>"
        f'<table class="kv">'
        f"<tr><td>Costs now</td><td>{_money(item.current_cost)} / month</td></tr>"
        f'<tr class="em"><td>Saving if applied</td><td>{_money(item.monthly_saving)} / month '
        f"({esc(item.annual_saving)} / year)</td></tr>"
        f"</table>{form}</div>"
    )


def _section(title: str, lede: str, body: str) -> str:
    if not body:
        return ""
    return (
        f'<section class="card"><h2>{esc(title)}</h2>'
        f"{f'<p class=lede>{esc(lede)}</p>' if lede else ''}{body}</section>"
    )


def render_report(
    view: ReportView, *, decidable: bool = False, verifies_identity: bool = False
) -> str:
    """One client's report as a page.

    Args:
        view: The arranged report. Every figure on it is a string the report rendered.
        decidable: Whether to offer the decision form on pending items. Off for a
            static render, because a form that posts nowhere is worse than no form.
        verifies_identity: Whether an authenticator stands behind the approver field.
            When none does, the form says the name will be recorded as unverified.
    """
    body: list[str] = [_banner(view.banner)]

    body.append(
        _section(
            "Executive summary",
            "",
            _kv(view.summary_rows)
            + f'<p class="prose model">{esc(view.executive_summary)}</p>'
            + '<p class="caveat">The paragraph above was written by a language model from '
            "the figures in the table. Every figure here is arithmetic over provider "
            "data; no number in it came from a language model.</p>",
        )
    )

    if view.pending:
        body.append(
            _section(
                "Awaiting your decision",
                NO_EXECUTION_NOTE,
                "".join(
                    _pending_item(one, decidable=decidable, verifies_identity=verifies_identity)
                    for one in view.pending
                ),
            )
        )

    body.append(
        _section(
            "Recommended actions",
            "Ordered by a weighted composite of the saving, how safe the action is and "
            "the detector's confidence — not by saving alone.",
            "".join(_finding_item(one, show_rank=True) for one in view.findings),
        )
    )
    body.append(
        _section(
            "Worth investigating — saving unknown",
            "Real findings with no figure we could derive. Excluded from the savings "
            "total rather than counted as zero, and listed by name so they are visible.",
            "".join(_finding_item(one, show_rank=False) for one in view.unquantified),
        )
    )
    body.append(
        _section(
            "Flagged but exempt — no action proposed",
            "Real waste signals on resources your own tags mark as protected. The figure "
            "beside each one is what you are paying, not a saving available to you.",
            "".join(_exempt_item(one) for one in view.exempt),
        )
    )
    body.append(
        _section(
            "Spend changes — investigate",
            "Spend moved. This is not a list of waste and not a list of actions: "
            "CostSentinel proposes nothing here.",
            "".join(_anomaly_item(one) for one in view.anomalies),
        )
    )
    body.append(
        _section(
            "Spend we could not assess",
            "Days missing from a cost series are unknown, not zero.",
            f'<ul class="plain">{"".join(f"<li>{esc(one)}</li>" for one in view.unassessed)}</ul>'
            if view.unassessed
            else "",
        )
    )
    if view.notes:
        body.append(
            _section(
                "Notes",
                "",
                f'<ul class="plain">{"".join(f"<li>{esc(one)}</li>" for one in view.notes)}</ul>',
            )
        )
    body.append(_section("How this was produced", "", _kv(view.basis_rows)))

    return _page(
        f"Cost review — {view.client}",
        "".join(body),
        subtitle=f"{view.period} · generated {view.generated} · severity {view.severity}",
    )


def render_estate(view: EstateView) -> str:
    """The overview: who is in scope and what is waiting."""
    clients = "".join(
        f'<a class="client" href="/dashboard/client/{esc(one.client)}">{esc(one.client)} — '
        f"{len(one.findings) + len(one.unquantified)} finding(s), "
        f"{len(one.pending)} awaiting a decision</a>"
        for one in view.reports
    )
    body = [
        _banner(view.banner),
        _section(
            "Estate",
            "One page per client. Every figure on them comes from that client's report.",
            clients or "<p>No client is in scope.</p>",
        ),
        _section(
            "Awaiting a decision",
            NO_EXECUTION_NOTE,
            f"<p>{view.total_pending} gated action(s) across {len(view.reports)} client(s). "
            f'<a href="/dashboard/approvals">Open the queue</a>.</p>',
        ),
    ]
    return _page(
        "CostSentinel — operator view",
        "".join(body),
        subtitle=f"generated {view.generated}",
    )


def render_queue(
    items: Sequence[PendingView],
    *,
    decidable: bool = False,
    verifies_identity: bool = False,
    banner: str = "",
    generated: str = "",
) -> str:
    """The pending-approval queue across every paused sweep."""
    body = [
        _banner(banner),
        _section(
            "Awaiting a decision",
            NO_EXECUTION_NOTE,
            "".join(
                _pending_item(one, decidable=decidable, verifies_identity=verifies_identity)
                for one in items
            )
            or "<p>Nothing is awaiting a decision.</p>",
        ),
    ]
    return _page(
        "CostSentinel — pending approvals",
        "".join(body),
        subtitle=f"{len(items)} waiting · generated {generated}"
        if generated
        else f"{len(items)} waiting",
    )
