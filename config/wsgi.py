import os

from django.core.wsgi import get_wsgi_application

from service_supervisor.runtime import start_web_lease_guard

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
start_web_lease_guard()
