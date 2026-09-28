# WebSocket Deployment (ASGI + Daphne + Nginx)

These examples run `elearncore` from `/srv/elearncore` as the unprivileged `elearn` user. Store production secrets in `/etc/elearncore.env` with mode `0600` and root ownership.

## Prepare The Host

```bash
sudo apt update
sudo apt install python3-venv nginx
sudo useradd --system --home /srv/elearncore --shell /usr/sbin/nologin elearn
sudo install -d -o elearn -g elearn /srv/elearncore/staticfiles /srv/elearncore/assets
sudo chmod 0750 /srv/elearncore/assets
```

The local `assets` directory is private application storage. Do not add an Nginx alias for it. Production object storage should use private objects and signed URLs.

## Start Daphne

```bash
sudo cp deployment/websocket/elearncore-daphne.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now elearncore-daphne
sudo systemctl status elearncore-daphne --no-pager
```

## Enable Nginx

Install valid certificates for the configured host names, then enable the HTTPS configuration:

```bash
sudo cp deployment/websocket/nginx.conf /etc/nginx/sites-available/elearncore
sudo ln -s /etc/nginx/sites-available/elearncore /etc/nginx/sites-enabled/elearncore
sudo nginx -t
sudo systemctl reload nginx
```

Nginx redirects HTTP to HTTPS, serves collected static files, and proxies HTTP/WebSocket traffic to Daphne on loopback. Uploaded media is never served directly by this example.

## Collect Static Files

```bash
cd /srv/elearncore
sudo -u elearn .venv/bin/python manage.py collectstatic --noinput
```
