## Rule

Bot-filed: filed_by_kind is recorded and is not owner, or the description after leading whitespace starts with [flood-created]. Approved work: not archived, state not CANCELED or CANCELLED, and not bot-filed or intake-approved. Follow-up: a bot-filed item with an active parent relation from it to approved work, or an active blocks relation between it and approved work in either direction; related and duplicate_of do not count. Every other bot-filed item is new scope, held from flood export until the owner answers approve.

The tree read shows scope_class, intake_hold and a pending intake_decision; an owner principal answers it with answer_intake_decision.
