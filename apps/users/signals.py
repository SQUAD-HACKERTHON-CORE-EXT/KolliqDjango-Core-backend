'''
apps/users/signals.py — NEW FILE
====================================
Triggers wallet provisioning the moment a user registers.
Without this wired up, create_wallet_for_user never runs and users
get stuck with no DVA
'''


from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def provision_wallet(sender, instance, created, **kwargs):
    if created:
        from apps.wallets.tasks import create_wallet_for_user
        create_wallet_for_user.delay(str(instance.id))