"""Idempotent first-run setup: the ExCon superuser (APP_USERNAME / APP_PASSWORD), the
"excon" group, placeholder teams A-D (people on .invalid addresses, no logins) and the Test
team. Runs at every container start; changes nothing that already exists, except that the
superuser's password follows APP_PASSWORD."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from ops.delivery import test_team
from ops.models import Person, Team, audit


class Command(BaseCommand):
    help = "Create the ExCon login, group, placeholder teams and the Test team if missing."

    def handle(self, *args, **opts):
        Group.objects.get_or_create(name="excon")
        User = get_user_model()
        if settings.APP_PASSWORD:
            u, created = User.objects.get_or_create(username=settings.APP_USERNAME,
                                                    defaults={"is_staff": True, "is_superuser": True})
            if created or not u.check_password(settings.APP_PASSWORD):
                u.set_password(settings.APP_PASSWORD)
                u.is_staff = u.is_superuser = u.is_active = True
                u.save()
                self.stdout.write(f"ExCon login {u.username} {'created' if created else 'password updated'}")
        else:
            self.stdout.write("APP_PASSWORD not set: no ExCon login created")
        if not Team.objects.filter(is_test=False).exists():
            for n, letter in enumerate("ABCD"):
                t = Team.objects.create(code=letter.lower(), name=f"Team {letter}", sort=n)
                Person.objects.create(name=f"Team {letter} Leader (placeholder)", role="tl", team=t,
                                      email=f"team-{letter.lower()}-tl@placeholder.invalid")
                Person.objects.create(name=f"Team {letter} Member (placeholder)", team=t,
                                      email=f"team-{letter.lower()}-1@placeholder.invalid")
            audit("seed", "placeholder teams A-D")
            self.stdout.write("Placeholder teams A-D created")
        test_team()
