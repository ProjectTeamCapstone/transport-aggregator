# Deployment & Sort Fix — Task List

## Frontend default sort (date & time ascending)
- [x] Add `time` sort option to `src/api.py` (ORDER BY departure_date, departure_hour)
- [x] Make `time` the default sort in `src/api.py` `/search` endpoint
- [x] Add "Date & time" option to the sort dropdown in `src/ui/index.html` (default selected)

## Vercel deployment fixes (commit working-tree changes)
- [x] Commit `api/index.py` (sys.path handling)
- [x] Commit `src/__init__.py` (importable package)
- [x] Commit slimmed `requirements.txt` (runtime-only)
- [x] Commit `requirements-dev.txt` (training env)
- [x] Commit `vercel.json` (add /health route)

## Git + Deploy
- [x] Commit all changes with a clear message
- [ ] Push to GitHub `giftwarieta/transport`
- [ ] Install Vercel CLI and deploy to `fare-checker.vercel.app`
- [ ] Verify `/health`, `/routes`, `/search` on the live site
