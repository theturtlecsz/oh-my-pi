# Amended acceptance criteria do not persist

I amended the scope and acceptance criteria on an open work item through the
workflow backend. The tool said the revision landed, and the revision number
went up, but when I read the item back the old scope and the old criteria were
still there. Only the description I passed stuck. It looks like `reviseWork`
drops everything except the description unless I restate it, so an amendment
that touches only `scope` and `acceptance_criteria` silently reverts to the
previous revision's values.

Please make `reviseWork` preserve the previous revision's `title`, `description`,
`scope`, and `acceptance_criteria` for every field the caller did not amend, so a
scope-only or criteria-only amendment keeps the untouched fields and bumps the
revision number.
