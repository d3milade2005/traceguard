# Local frontend reads

GitHub integration is deferred for this frontend increment. Opt in with
DEV_READ_ACCESS=true and APP_ENV=development in the local API environment.
The flag defaults false and is rejected in test/production. It permits only
operational GET/HEAD requests; auth routes and all writes retain authentication.

Two read endpoints expose already-persisted evidence using RunService:
GET /api/v1/runs/{id}/trace returns CanonicalTrace (409 if not normalized).
GET /api/v1/runs/{id}/original-trace returns the unchanged submitted JSON bytes.
Both retain normal authentication unless the explicit development flag is enabled.
No provider call, normalization, or context selection runs on these reads.
The attribution context_manifest remains the authoritative historical selection;
excerpt bodies were not persisted and cannot be reconstructed by the UI.
