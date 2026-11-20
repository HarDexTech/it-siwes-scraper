-- Limited role for the website (Render). Create the role first in the Neon
-- console (Roles -> New role -> siwes_web), then run this as the owner role.
-- The site can read listings and run history, and can only write requested fields.
GRANT USAGE ON SCHEMA public TO siwes_web;
GRANT SELECT ON placements, collector_runs TO siwes_web;
GRANT SELECT, INSERT, UPDATE ON requested_fields TO siwes_web;
GRANT SELECT ON companies TO siwes_web;
GRANT SELECT, INSERT, UPDATE ON requested_pairs TO siwes_web;
