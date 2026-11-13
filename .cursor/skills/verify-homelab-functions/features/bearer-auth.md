# Bearer auth

Bearer auth is how machine callers reach protected AccessVector routes.
Missing or wrong tokens must fail. The ephemeral broker token must succeed
on a route that does not need Home Assistant.

## Sub-features

- `bearer-missing` rejects `GET /v1/catalog/services` with 401 when the
  Authorization header is absent.
- `bearer-wrong` rejects the same route with 401 when the bearer is not
  the broker token.
- `bearer-ring` rejects the same route with 401 when the bearer is the
  Pebble ring token.
- `bearer-ok` returns the seeded catalog with the ephemeral broker token.

## How to get to it (user POV)

- GET `http://127.0.0.1:<port>/v1/catalog/services` with no Authorization
  header.
- GET the same path with `Authorization: Bearer <wrong>`.
- GET the same path with `Authorization: Bearer $PEBBLE_RING_TOKEN`.
- GET the same path with `Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN`.
- Run `./.venv/bin/python .cursor/skills/verify-homelab-functions/verify.py`
  and read `check bearer-unauth` and `check bearer-ok`.

## Driving it with verify.py

Preconditions:

- `verify.py` printed `doctor health status=ok` and `catalog=verify-stub`.
- Scratch catalog file `service-catalog.json` contains `verify-stub`.
- The helper generated the broker token. Do not paste it into the
  transcript.

- **Missing token.** GET `/v1/catalog/services` with no bearer. The helper
  does this in `check bearer-unauth`. Status `401`. JSON error code is
  `unauthorized`.
- **Wrong token.** GET `/v1/catalog/services` with `Authorization: Bearer
  wrong-token`. Status `401`. Error code is `unauthorized`.
- **Ring token.** GET `/v1/catalog/services` with the ephemeral Pebble ring
  token. Status `401`. Error code is `unauthorized`.
- **Valid token.** GET `/v1/catalog/services` with the ephemeral broker
  bearer. The helper does this in `check bearer-ok`. Status `200`. JSON
  `schema` is `homelab-service-catalog.v1`. First service name is
  `verify-stub`.
- **Proof.** Stdout contains `check bearer-unauth PASS` with
  `missing=401 wrong=401 ring=401` and `check bearer-ok PASS`. Evidence
  must not contain the bearer value.

## Gotchas

- `/health` and `/docs` stay public. Using them as the 200 proof does not
  exercise bearer auth.
- `PEBBLE_RING_TOKEN` is a different secret. `check bearer-unauth` requires
  that the ring token returns 401 on `/v1/catalog/services`.
- Catalog files live in the helper scratch directory. A 503 means the
  seeded file is missing, not that auth failed.
- Never print the generated bearer. The helper redacts `Bearer` values.
