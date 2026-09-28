sudo cp deployment/websocket/elearncore-daphne.service /etc/systemd/system/elearncore-daphne.service
sudo systemctl daemon-reload
sudo systemctl enable elearncore-daphne
sudo systemctl start elearncore-daphne

sudo cp deployment/websocket/nginx.conf /etc/nginx/sites-available/elearncore
sudo ln -s /etc/nginx/sites-available/elearncore /etc/nginx/sites-enabled/elearncore
sudo nginx -t
sudo systemctl restart nginx