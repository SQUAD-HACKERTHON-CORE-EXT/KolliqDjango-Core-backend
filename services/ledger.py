"""
services/ledger.py — Kolliq Immutable Ledger
=============================================

RULES:
  1. Every balance change MUST go through this module — never call
     wallet.balance += x directly in views or tasks.

  2. Every operation is IDEMPOTENT — pass an idempotency_key and if
     the same key is seen again, the first result is returned silently.
     This makes Paystack webhook retries and Celery retries safe.

  3. Transaction records are APPEND-ONLY — never updated, never deleted.
     Reversals create a new REVERSAL row; the original stays forever.

  4. All wallet mutations use SELECT FOR UPDATE — no race conditions
     even with multiple Celery workers.

  5. The balance on Wallet is a CACHED SUM — always derivable from
     Transaction records. Use verify_wallet_balance() to check integrity.

HOW INBOUND PAYMENTS WORK (no Transfers API involved):
  Paystack DVA ──charge.success webhook──► ledger.credit_inbound()
  That's it. The money is already in Kolliq's Paystack account.
  We just update our DB ledger to record that the user's slice grew.

HOW OUTBOUND WORKS (Transfers API):
  User requests withdrawal ──► ledger.debit_for_withdrawal()
    → wallet debited, WithdrawalRequest created (PENDING)
  Admin approves ──► payout_tasks.process_withdrawal()
    → Paystack Transfer API called with idempotency key
  transfer.success webhook ──► ledger.complete_withdrawal()
    → WithdrawalRequest marked COMPLETED, reversal created if needed
"""

import logging
from decimal import Decimal
from django.db import transaction as db_tx
from django.conf import settings

logger = logging.getLogger(__name__)


# ── Idempotency helper ────────────────────────────────────────────────────────

def _already_processed(idempotency_key: str, tx_type: str) -> bool:
    """
    Returns True if a Transaction with this idempotency_key already exists.
    Prevents double-crediting on webhook retries.
    """
    from apps.payments.models import Transaction
    return Transaction.objects.filter(
        idempotency_key=idempotency_key,
        transaction_type=tx_type,
    ).exists()


# ── INBOUND: DVA payment received ────────────────────────────────────────────

def credit_inbound(
    wallet_id: str,
    amount: Decimal,
    paystack_reference: str,
    narration: str = '',
    channel: str = 'dedicated_nuban',
    paid_at: str = '',
) -> dict:
    """
    Credit a user's wallet from an inbound DVA payment.
    Called by webhook handler when charge.success fires on a personal DVA.

    Idempotency: paystack_reference is used as the idempotency key.
    If this reference has already been processed, returns immediately.

    Race-safe: uses SELECT FOR UPDATE on the wallet row.

    Returns: { 'credited': bool, 'amount': Decimal, 'new_balance': Decimal }
    """
    from apps.wallets.models import Wallet
    from apps.payments.models import Transaction

    idempotency_key = f'inbound:{paystack_reference}'

    if _already_processed(idempotency_key, Transaction.Type.CREDIT):
        logger.info(f'credit_inbound: already processed {paystack_reference}')
        return {'credited': False, 'reason': 'duplicate'}

    with db_tx.atomic():
        # SELECT FOR UPDATE prevents concurrent webhooks crediting same wallet
        wallet = Wallet.objects.select_for_update().get(id=wallet_id)

        wallet.balance += amount
        wallet.save(update_fields=['balance', 'updated_at'])

        Transaction.objects.create(
            user=wallet.user,
            transaction_type=Transaction.Type.CREDIT,
            amount=amount,
            status=Transaction.Status.SUCCESS,
            paystack_reference=paystack_reference,
            idempotency_key=idempotency_key,
            description=f'Wallet top-up via {channel}',
            metadata={
                'narration': narration,
                'channel': channel,
                'paid_at': paid_at,
            },
        )

    logger.info(f'credit_inbound: ₦{amount} → wallet={wallet_id} ref={paystack_reference}')
    return {'credited': True, 'amount': amount, 'new_balance': wallet.balance}


# ── INBOUND: Escrow DVA payment received ─────────────────────────────────────

def credit_escrow_inbound(
    employer_wallet_id: str,
    job_id: str,
    amount: Decimal,
    paystack_reference: str,
    narration: str = '',
) -> dict:
    """
    Credit an employer's escrow_balance from an inbound escrow DVA payment.
    Called by escrow.match_escrow_payment_to_job() after the job is matched.

    Idempotency: paystack_reference prevents double-crediting if webhook retries.
    """
    from apps.wallets.models import Wallet
    from apps.payments.models import Transaction

    idempotency_key = f'escrow_in:{paystack_reference}'

    if _already_processed(idempotency_key, Transaction.Type.ESCROW_HOLD):
        logger.info(f'credit_escrow_inbound: already processed {paystack_reference}')
        return {'credited': False, 'reason': 'duplicate'}

    with db_tx.atomic():
        wallet = Wallet.objects.select_for_update().get(id=employer_wallet_id)

        wallet.escrow_balance += amount
        wallet.save(update_fields=['escrow_balance', 'updated_at'])

        Transaction.objects.create(
            user=wallet.user,
            transaction_type=Transaction.Type.ESCROW_HOLD,
            amount=amount,
            status=Transaction.Status.SUCCESS,
            paystack_reference=paystack_reference,
            idempotency_key=idempotency_key,
            description='Escrow funded via bank transfer',
            metadata={
                'job_id': job_id,
                'narration': narration,
            },
        )

    logger.info(
        f'credit_escrow_inbound: ₦{amount} → escrow wallet={employer_wallet_id} '
        f'job={job_id} ref={paystack_reference}'
    )
    return {'credited': True, 'amount': amount, 'new_escrow_balance': wallet.escrow_balance}


# ── INTERNAL: Escrow release (job completion) ─────────────────────────────────

def release_escrow(
    job_id: str,
    employer_wallet_id: str,
    worker_wallet_id: str,
    gross: Decimal,
    platform_fee_percent: Decimal,
) -> dict:
    """
    Double-entry escrow release on job completion.
    No Paystack call needed — pure internal ledger.

    Entries created (all in one atomic block):
      DEBIT:  employer.escrow_balance  -= gross          (ESCROW_RELEASE)
      CREDIT: worker.balance           += gross - fee    (CREDIT)
      CREDIT: platform_wallet.balance  += fee            (PLATFORM_FEE)

    Idempotency key: f'escrow_release:{job_id}:{worker_wallet_id}'
    """
    from apps.wallets.models import Wallet
    from apps.payments.models import Transaction
    from apps.jobs.models import Job

    fee = (gross * platform_fee_percent / Decimal('100')).quantize(Decimal('0.01'))
    net_to_worker = gross - fee

    idempotency_key = f'escrow_release:{job_id}:{worker_wallet_id}'

    if _already_processed(idempotency_key, Transaction.Type.ESCROW_RELEASE):
        logger.info(f'release_escrow: already processed job={job_id} worker_wallet={worker_wallet_id}')
        return {'released': False, 'reason': 'duplicate'}

    with db_tx.atomic():
        # Lock all three wallets in consistent order to prevent deadlocks
        wallet_ids = sorted([
            employer_wallet_id,
            worker_wallet_id,
            str(settings.ARISE_WALLET_ID),
        ])
        wallets = {
            str(w.id): w
            for w in Wallet.objects.select_for_update().filter(id__in=wallet_ids)
        }

        employer_wallet = wallets[employer_wallet_id]
        worker_wallet = wallets[worker_wallet_id]

        if employer_wallet.escrow_balance < gross:
            raise ValueError(
                f'Insufficient escrow: have ₦{employer_wallet.escrow_balance}, need ₦{gross}'
            )

        job = Job.objects.get(id=job_id)

        # 1. Debit employer escrow
        employer_wallet.escrow_balance -= gross
        employer_wallet.save(update_fields=['escrow_balance', 'updated_at'])

        # 2. Credit worker
        worker_wallet.balance += net_to_worker
        worker_wallet.save(update_fields=['balance', 'updated_at'])

        # 3. Credit platform
        platform_wallet_id = str(settings.ARISE_WALLET_ID)
        if platform_wallet_id in wallets:
            platform_wallet = wallets[platform_wallet_id]
            platform_wallet.balance += fee
            platform_wallet.save(update_fields=['balance', 'updated_at'])

        # 4. Append-only transaction records (3 entries, one atomic block)
        Transaction.objects.bulk_create([
            Transaction(
                user=employer_wallet.user,
                transaction_type=Transaction.Type.ESCROW_RELEASE,
                amount=gross,
                status=Transaction.Status.SUCCESS,
                idempotency_key=idempotency_key,
                job=job,
                related_user=worker_wallet.user,
                description=f'Escrow released: {job.title}',
            ),
            Transaction(
                user=worker_wallet.user,
                transaction_type=Transaction.Type.CREDIT,
                amount=net_to_worker,
                status=Transaction.Status.SUCCESS,
                idempotency_key=f'worker_pay:{job_id}:{worker_wallet_id}',
                job=job,
                related_user=employer_wallet.user,
                description=f'Payment for: {job.title}',
            ),
            Transaction(
                user=employer_wallet.user,
                transaction_type=Transaction.Type.PLATFORM_FEE,
                amount=fee,
                status=Transaction.Status.SUCCESS,
                idempotency_key=f'platform_fee:{job_id}:{worker_wallet_id}',
                job=job,
                description=f'Platform fee ({platform_fee_percent}%): {job.title}',
            ),
        ])

    logger.info(
        f'release_escrow: job={job_id} gross=₦{gross} '
        f'net=₦{net_to_worker} fee=₦{fee}'
    )
    return {
        'released': True,
        'gross': gross,
        'net_to_worker': net_to_worker,
        'platform_fee': fee,
        'worker_new_balance': worker_wallet.balance,
    }


# ── OUTBOUND: Withdrawal debit (step 1 of 2) ─────────────────────────────────

def debit_for_withdrawal(
    wallet_id: str,
    amount: Decimal,
    withdrawal_request_id: str,
) -> dict:
    """
    Debit user wallet when they request a withdrawal.
    Creates a PENDING Transaction record.
    The balance is debited immediately so the user can't spend it twice.

    Idempotency: withdrawal_request_id prevents double-debit on retries.
    """
    from apps.wallets.models import Wallet
    from apps.payments.models import Transaction

    idempotency_key = f'withdrawal_debit:{withdrawal_request_id}'

    if _already_processed(idempotency_key, Transaction.Type.DEBIT):
        logger.info(f'debit_for_withdrawal: already debited {withdrawal_request_id}')
        return {'debited': False, 'reason': 'duplicate'}

    with db_tx.atomic():
        wallet = Wallet.objects.select_for_update().get(id=wallet_id)

        if wallet.balance < amount:
            raise ValueError(
                f'Insufficient balance: ₦{wallet.balance} available, ₦{amount} requested'
            )

        wallet.balance -= amount
        wallet.save(update_fields=['balance', 'updated_at'])

        Transaction.objects.create(
            user=wallet.user,
            transaction_type=Transaction.Type.DEBIT,
            amount=amount,
            status=Transaction.Status.PENDING,   # PENDING until Paystack confirms
            idempotency_key=idempotency_key,
            description=f'Withdrawal request #{str(withdrawal_request_id)[:8]}',
            metadata={'withdrawal_id': withdrawal_request_id},
        )

    logger.info(f'debit_for_withdrawal: ₦{amount} from wallet={wallet_id}')
    return {'debited': True, 'amount': amount, 'new_balance': wallet.balance}


def complete_withdrawal(
    withdrawal_request_id: str,
    paystack_reference: str,
) -> dict:
    """
    Mark the withdrawal DEBIT as SUCCESS once Paystack transfer.success fires.
    Updates the PENDING Transaction to SUCCESS (only status update ever allowed).
    """
    from apps.payments.models import Transaction

    idempotency_key = f'withdrawal_debit:{withdrawal_request_id}'
    updated = Transaction.objects.filter(
        idempotency_key=idempotency_key,
        status=Transaction.Status.PENDING,
    ).update(
        status=Transaction.Status.SUCCESS,
        paystack_reference=paystack_reference,
    )

    if updated:
        logger.info(f'complete_withdrawal: {withdrawal_request_id} marked SUCCESS')
    return {'updated': bool(updated)}


def reverse_withdrawal(
    wallet_id: str,
    amount: Decimal,
    withdrawal_request_id: str,
    reason: str = '',
) -> dict:
    """
    Refund a failed/reversed withdrawal back to the user's wallet.
    Creates a new REVERSAL transaction — the original DEBIT stays forever.

    Called by webhook handler on transfer.failed / transfer.reversed.
    Idempotency: prevents double-refund if webhook retries.
    """
    from apps.wallets.models import Wallet
    from apps.payments.models import Transaction

    idempotency_key = f'withdrawal_reversal:{withdrawal_request_id}'

    if _already_processed(idempotency_key, Transaction.Type.REVERSAL):
        logger.info(f'reverse_withdrawal: already reversed {withdrawal_request_id}')
        return {'reversed': False, 'reason': 'duplicate'}

    with db_tx.atomic():
        wallet = Wallet.objects.select_for_update().get(id=wallet_id)

        wallet.balance += amount
        wallet.save(update_fields=['balance', 'updated_at'])

        # Mark original DEBIT as FAILED
        Transaction.objects.filter(
            idempotency_key=f'withdrawal_debit:{withdrawal_request_id}',
        ).update(status=Transaction.Status.FAILED)

        # New REVERSAL record — the DEBIT row is never deleted
        Transaction.objects.create(
            user=wallet.user,
            transaction_type=Transaction.Type.REVERSAL,
            amount=amount,
            status=Transaction.Status.SUCCESS,
            idempotency_key=idempotency_key,
            description=f'Withdrawal refunded: {reason or "transfer failed"}',
            metadata={
                'withdrawal_id': withdrawal_request_id,
                'reason': reason,
            },
        )

    logger.info(
        f'reverse_withdrawal: ₦{amount} refunded to wallet={wallet_id} '
        f'withdrawal={withdrawal_request_id}'
    )
    return {'reversed': True, 'amount': amount, 'new_balance': wallet.balance}


# ── AUDIT: verify balance integrity ──────────────────────────────────────────

def verify_wallet_balance(wallet_id: str) -> dict:
    """
    Recompute a wallet's balance from its Transaction history.
    Use this for audits, reconciliation, or debugging.

    Credits add to balance, debits/fees/escrow_release subtract.
    Returns any discrepancy between cached balance and computed balance.
    """
    from apps.wallets.models import Wallet
    from apps.payments.models import Transaction
    from django.db.models import Sum

    wallet = Wallet.objects.get(id=wallet_id)
    txns = Transaction.objects.filter(
        user=wallet.user,
        status=Transaction.Status.SUCCESS,
    )

    CREDIT_TYPES = {
        Transaction.Type.CREDIT,
        Transaction.Type.REVERSAL,
    }
    DEBIT_TYPES = {
        Transaction.Type.DEBIT,
        Transaction.Type.PLATFORM_FEE,
        Transaction.Type.SAVINGS_DEPOSIT,
        Transaction.Type.INSURANCE_PREMIUM,
        Transaction.Type.LOAN_REPAYMENT,
    }

    credits = txns.filter(transaction_type__in=CREDIT_TYPES).aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0')

    debits = txns.filter(transaction_type__in=DEBIT_TYPES).aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0')

    computed = credits - debits
    cached = wallet.balance
    discrepancy = cached - computed

    result = {
        'wallet_id': wallet_id,
        'cached_balance': str(cached),
        'computed_balance': str(computed),
        'discrepancy': str(discrepancy),
        'match': discrepancy == Decimal('0'),
        'credits_total': str(credits),
        'debits_total': str(debits),
    }

    if discrepancy != Decimal('0'):
        logger.error(
            f'LEDGER MISMATCH: wallet={wallet_id} '
            f'cached=₦{cached} computed=₦{computed} diff=₦{discrepancy}'
        )

    return result