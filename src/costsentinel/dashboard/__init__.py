"""The operator surface: a read-only view of what a sweep found.

Everything here renders a :class:`~costsentinel.domain.report.ClientReport` -- the same
object the Markdown renderer turns into a document -- so the page an operator reads and
the report a client reads cannot show different things about one sweep. The view layer
decides order and emphasis and computes no figure, which is asserted with the same
copy-never-compose machinery the rendered-report gate uses (ARCHITECTURE.md D123).

Read-only, with one exception handled through one path: the queue page can submit a
decision, and it does so by calling the same functions ``costsentinel approvals decide``
calls. There is no second decision implementation and no new way to mutate anything --
the four-way no-mutation invariant of Phase 10 is untouched (D118).

Composed in Python with no template engine and no external asset, for the reason D108
gave for the Markdown renderer: a template is a second place numbers get formatted, and
formatting is where a figure stops equalling its source.
"""
