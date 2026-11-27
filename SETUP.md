# Survey server – setup on your VPS

(Using GitHub? Follow `DEPLOY_GITHUB.md` instead of steps 1-3 below.)

Needs: a Linux VPS with Docker + Docker Compose, ports 80 and 443 open.

## 1. Copy the folder to the VPS
Upload this whole `survey_server` folder (e.g. to `/opt/survey`):
```
scp -r survey_server root@YOUR_VPS_IP:/opt/survey
ssh root@YOUR_VPS_IP
cd /opt/survey
```

## 2. Create the settings file
```
cp .env.example .env
nano .env
```
Fill in: `DB_PASSWORD`, `SECRET_KEY` (32+ random characters, e.g. `openssl rand -hex 32`),
`ADMIN_USER`, `ADMIN_PASSWORD`, and `DOMAIN`:
- **Testing, no domain yet:** `DOMAIN=:80`  -> works on `http://YOUR_VPS_IP`
- **Real use:** `DOMAIN=survey.yourcompany.com` (point a DNS A record at the VPS first);
  Caddy then gets the HTTPS certificate automatically.

## 3. Start
```
docker compose up -d --build
docker compose logs -f api        # Ctrl+C to leave; look for "Application startup complete"
```
Check: open `http://YOUR_VPS_IP/api/v1/health` (or your domain) -> `{"status":"ok"}`.

## 4. Admin page
Open `/admin/` on the same address, sign in with `ADMIN_USER` / `ADMIN_PASSWORD`.
- **Users:** add one login per field user (username + password, 8+ characters) and choose the
  project (PESCO / LESCO / none). A user with a project can read and send only that project's data;
  you can change it later in the Users table.
- **Project data (PESCO / LESCO):** pick the project, upload the Excel sheet (.xlsx or .csv with the
  header `Sub Division, Feeder, Location, TRF Code, Capacity(KVA), Reference No.` (**PESCO is keyed by TRF Code, no List Number needed**; LESCO still needs `List Number`); type the Circle and Division of the file in the two boxes, or add `Circle` / `Division` columns instead. Only List Number, Sub Division, Circle and Division are compulsory, the rest may be empty). Upload again any
  time to add or update rows (tick "remove List Numbers that are not in the file" to delete the
  others). Below it, edit the dropdown lists (TF rating = cable width, pole type, earthing); phones
  pick up changes the next time they download the project list.
- **Daily permission:** press "Give permission to all (today)" each morning (or "Give permission"
  per user). A user must open the app within 2 hours of that, otherwise the account shows LOCKED
  until you give permission again. Permission ends at midnight (Pakistan time). Change the rules in
  `.env`: `PERMISSION_REQUIRED`, `PERMISSION_OPEN_HOURS`, `TIMEZONE`. Admins are never locked.
  After the first deploy every existing surveyor shows "Not given yet" until you press the button.
- **Done / not-done:** for PESCO / LESCO it uses the uploaded sheet. For APMS / Single / Three upload
  a CSV with header `form_type,list_number`.
- **Data:** "Show data" lists every field in its own column (filter by List Number or Reference
  Number); CSV export per form type.
- **Photos (admin only):** download one List Number's or one Reference Number's photos, or all
  photos sorted into one folder per List Number / Reference Number. Surveyors cannot download anything.

## 5. Connect the phone
The server address is built into the app (`lib/config.dart` in the app project, or
`--dart-define=API_BASE_URL=...` when building): use `http://YOUR_VPS_IP` for testing or
`https://your-domain`. The app does NOT use port 8000; Caddy serves ports 80 / 443. Users only
type their username and password.

## Where things are stored
- `data/pg`    – Postgres database
- `data/files` – photos: `photos/<type>/<list number>/<user>__<form id>/*.webp`
Back up BOTH folders (plus `.env`). A copy of `data/` to your local replication server with
`rsync -a --ignore-existing`-style pulls keeps old files even after the server purges them.

## Purge after 6 months
Set `PURGE_ENABLED=true` in `.env` and run `docker compose up -d`. Forms older than
`RETENTION_DAYS` (183) are then deleted daily from this server. Turn it on only after your
local replica is running and tested.

## Useful commands
```
docker compose ps
docker compose logs --tail 100 api
docker compose restart api
docker compose down              # stop (data stays)
```
