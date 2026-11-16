# Messaging database migrations

Run `squire-messaging-migrate` as a dedicated release step before starting API/workers.
Revision 1 creates the versioned schema in `messaging.models`; a PostgreSQL advisory
transaction lock serializes migration. API startup verifies the revision and does
not run `create_all` unless the explicit test/development auto-migrate flag is set.
Unknown revisions fail closed. Before future schema changes add an ordered upgrade
function and a restore/forward-repair test; never mutate revision 1 on a live database.
