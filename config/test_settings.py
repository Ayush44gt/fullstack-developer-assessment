from .settings import *  # noqa: F401,F403
from .settings import INSTALLED_APPS, MIDDLEWARE

# Silk adds its own queries to every request, which would skew the query-count
# assertions in orders/tests. Profiling is done by `manage.py profile_summary`.
ENABLE_SILK = False
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "silk"]
MIDDLEWARE = [mw for mw in MIDDLEWARE if not mw.startswith("silk.")]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
