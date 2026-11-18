import getpass
from django.contrib.auth import get_user_model
from django.contrib.auth.management.commands.changepassword import Command as ChangePassword
from django.db.models import F


class Command(ChangePassword):
    def handle(self, *args, **options):
        result = super().handle(*args, **options)
        get_user_model().objects.using(options['database']).filter(username=options.get('username') or getpass.getuser()).update(
            password_change_required=True, session_revision=F('session_revision')+1, online_until=None)
        return result
