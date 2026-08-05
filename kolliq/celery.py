import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'kolliq.settings')

app = Celery('kolliq')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
app.autodiscover_tasks(related_name='payout_tasks')  # also finds payout_tasks.py files
app.autodiscover_tasks(related_name='admin_notify')  # also finds admin_notify.py, since WithdrawalRequestView imports notify_admin_large_withdrawal from there too


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')