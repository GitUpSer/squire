# Messaging database migrations

Run `squire-messaging-migrate` as a dedicated release step before starting API/workers.
Revision 1 creates the versioned schema in `messaging.models`; a PostgreSQL advisory
transaction lock serializes migration. API startup verifies the revision and does
not run `create_all` unless the explicit test/development auto-migrate flag is set.
Unknown revisions fail closed. Before future schema changes add an ordered upgrade
function and a restore/forward-repair test; never mutate revision 1 on a live database.

Revision 2 adds trusted inbound expiry and outbound parent-event identity. Pause
capture and stop API/workers before upgrading. Old message rows have unknown
expiry and receive deadline zero. Legacy runtime outbound payloads without a
challenge are quarantined: queued/claimed rows cancel; dispatching/uncertain rows
stay uncertain. This includes old runtime introductions; explicit phone challenges
are preserved. No ambiguous send is retried. Deploy the expiry-aware transport and
runtime before resuming capture; message events must supply `expires_at` explicitly.
