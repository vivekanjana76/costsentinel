"""Report rendering: a :class:`~costsentinel.domain.report.ClientReport` as a document.

The guarantee this package exists to extend is the one that previously stopped a layer
short of the output. Every figure on a ``ClientReport`` is checked -- the evaluation
harness asserts each one against committed ground truth and traces every model-written
sentence back to state -- and until Phase 8 the *rendered* document was checked by
nothing, while being the only artefact a client ever reads.

So rendering is deliberately dull. No template engine: a report with five fixed
sections is a function, and a template is one more place a figure could be reformatted
on the way out. Every monetary value is rendered through
:meth:`~costsentinel.domain.common.MoneyAmount.display`, the same call the terminal
digest and the audit trail use, so "the number in the document equals the number in
the object" is true by construction and asserted anyway.

See ROADMAP.md Phase 8.
"""
