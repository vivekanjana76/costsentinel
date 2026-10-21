"""Writing the operator surface to files.

A page is easier to review, screenshot and commit than a server is to run. This writes
the same HTML the routes serve -- the same view model, the same renderer -- to a
directory of files that open over ``file://`` with no process listening and no network.

Three reasons it exists rather than being a convenience:

* the committed screenshots in ``docs/`` are taken from these files, so what a
  repository visitor sees is what the surface renders rather than a mock-up;
* a reviewer can look at the output of a change without starting anything;
* the decision form is omitted, because a form that posts nowhere is worse than no
  form -- a static page says plainly that it is a view.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from costsentinel.config import Settings
from costsentinel.dashboard.app import estate, pending_queue, stored_reports
from costsentinel.dashboard.html import render_estate, render_queue, render_report
from costsentinel.observability.logging import get_logger

_log = get_logger("dashboard.static")

#: Filenames inside the output directory. Stable, so a link in a document or a
#: screenshot script does not move between runs.
INDEX = "index.html"
APPROVALS = "approvals.html"


def client_filename(client: str) -> str:
    """A stable filename for one client's page."""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in client)
    return f"client-{safe}.html"


def write_static(
    settings: Settings, directory: Path, *, generated_at: datetime | None = None
) -> list[Path]:
    """Write the whole surface to ``directory``.

    Returns:
        Every path written, the overview first.
    """
    directory.mkdir(parents=True, exist_ok=True)
    views = stored_reports(settings)
    written: list[Path] = []

    overview = directory / INDEX
    overview.write_text(
        render_estate(estate(settings, generated_at=generated_at)), encoding="utf-8"
    )
    written.append(overview)

    for view in views:
        path = directory / client_filename(view.client)
        # `decidable=False`: a static page has nowhere to post to, and offering a
        # button that does nothing would be worse than offering none.
        path.write_text(render_report(view, decidable=False), encoding="utf-8")
        written.append(path)

    queue = directory / APPROVALS
    queue.write_text(
        render_queue(
            pending_queue(settings),
            decidable=False,
            banner=next((one.banner for one in views if one.banner), ""),
            generated=(generated_at or datetime.now().astimezone()).date().isoformat(),
        ),
        encoding="utf-8",
    )
    written.append(queue)

    _log.info("dashboard written", extra={"directory": str(directory), "pages": len(written)})
    return written
