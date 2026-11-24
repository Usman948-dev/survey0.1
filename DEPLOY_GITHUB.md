# Put the server on GitHub and run it on the VPS (157.173.96.166)

`.env` (passwords) and `data/` (database + photos) are in `.gitignore`: they never go to GitHub.
Make the GitHub repository **private**.

## A. One time: send this folder to GitHub (on your PC)
```
cd survey_server
git init -b main
git add .
git commit -m "Survey server"
git remote add origin https://github.com/YOUR_NAME/survey-server.git
git push -u origin main
```
(Create the empty private repo `survey-server` on github.com first. Or upload the files in the
browser: "Add file -> Upload files".)

## B. One time: get it onto the VPS (ssh root@157.173.96.166)
```
# 1. let the VPS read the private repo: create a deploy key
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub        # copy this line
```
GitHub -> your repo -> Settings -> Deploy keys -> Add deploy key -> paste (read-only is enough).
```
printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
ssh -T git@github.com               # answer yes; "Hi ...! You've successfully authenticated"

# 2. get the code
git clone git@github.com:YOUR_NAME/survey-server.git /opt/survey
cd /opt/survey

# 3. settings (your real secrets, only on the VPS)
cp .env.example .env
nano .env
```
Set in `.env`:
- `DOMAIN=:80`   (plain http on the IP. When you have a domain: `DOMAIN=survey.yourcompany.com`, https is automatic)
- `DB_PASSWORD=` a long random password
- `SECRET_KEY=` 32+ random characters (`openssl rand -hex 32`)
- `ADMIN_USER=` / `ADMIN_PASSWORD=` your admin login
```
# 4. start
docker compose up -d --build
curl http://157.173.96.166/api/v1/health      # -> {"status":"ok"}
```
Open `http://157.173.96.166/admin/` and sign in with the admin login.

## C. Every update afterwards
Change code on your PC -> `git add . && git commit -m "..." && git push`, then on the VPS:
```
cd /opt/survey && ./deploy.sh
```
**Automatic instead:** add the three secrets named at the top of `.github/workflows/deploy.yml`
(`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`) and every push to `main` runs the tests and deploys by itself.

## IMPORTANT if the server already holds data
The compose file here keeps the database in `./data/pg`. If your running server was started from a
compose file that uses a Docker volume (`pgdata`), switching would start with an EMPTY database.
Check first: `docker volume ls` and `docker compose ps`. If real surveys are already stored,
back up (`docker compose exec db pg_dump -U survey survey > backup.sql`) and tell me before you
switch. If the server is new / empty, just follow the steps above.

## Firewall
Ports 80 and 443 must be open (`ufw allow 80 && ufw allow 443`). Port 8000 is not used from outside.
