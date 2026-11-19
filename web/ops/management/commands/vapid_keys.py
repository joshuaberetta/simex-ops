"""Print a new VAPID private key for push notifications (set it as the VAPID_PRIVATE_KEY secret)."""

import base64

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate a VAPID private key for SIMEX Mail push notifications."

    def handle(self, *args, **opts):
        from cryptography.hazmat.primitives.asymmetric import ec
        n = ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value
        self.stdout.write("VAPID_PRIVATE_KEY=" + base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode())
        self.stdout.write("Changing it later makes every device subscribe again (on its next visit).")
