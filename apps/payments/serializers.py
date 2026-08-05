from rest_framework import serializers
from .models import Transaction


class TransactionSerializer(serializers.ModelSerializer):
    job_title = serializers.CharField(source='job.title', read_only=True, allow_null=True)
    related_user_phone = serializers.CharField(
        source='related_user.phone', read_only=True, allow_null=True
    )

    class Meta:
        model = Transaction
        fields = [
            'id', 'transaction_type', 'amount', 'status',
            'job_title', 'related_user_phone',
            'description', 'paystack_reference',
            'created_at', 'idempotency_key', 'metadata',
        ]