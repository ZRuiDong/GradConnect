"""Parse the explicitly supported PostgreSQL URL options without opening a connection."""
from urllib.parse import parse_qs, unquote, urlparse
from django.core.exceptions import ImproperlyConfigured


def postgres_config(value):
    try:
        url = urlparse(value)
        port = url.port or 5432
        if url.scheme not in {'postgres', 'postgresql'} or not url.path.lstrip('/') or url.fragment:
            raise ValueError
        options = parse_qs(url.query, keep_blank_values=True)
        allowed = {'sslmode', 'sslrootcert', 'connect_timeout', 'application_name'}
        if options.keys()-allowed or any(len(values) != 1 or not values[0] for values in options.values()):
            raise ValueError
        options = {key: values[0] for key, values in options.items()}
        if 'sslmode' in options and options['sslmode'] not in {'disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'}:
            raise ValueError
        timeout = int(options.get('connect_timeout', 5))
        if not 2 <= timeout <= 120:
            raise ValueError
    except (ValueError, TypeError):
        raise ImproperlyConfigured('DATABASE_URL无效；请指定PostgreSQL数据库，支持sslmode、sslrootcert、connect_timeout（2–120秒）及application_name，且参数不可重复。') from None
    return {'ENGINE': 'django.db.backends.postgresql', 'NAME': unquote(url.path.lstrip('/')),
        'USER': unquote(url.username or ''), 'PASSWORD': unquote(url.password or ''),
        'HOST': url.hostname or '', 'PORT': port, 'CONN_MAX_AGE': 60, 'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {**options, 'connect_timeout': timeout, 'application_name': options.get('application_name', 'gradconnect')}}
