user = 'www-data'
group = 'www-data'
logfile = '/var/www/twweb/logs/gunicorn.log'
workers = 3
loglevel = 'info'
bind = '127.0.0.1:8040'
timeout = 15
