'''
apps/jobs/views.py — FINAL, fully documented
=============================
- Escrow imports fixed (services.escrow, not apps.payments.escrow)
- JobCreateView charges the tiered job-creation fee upfront
- Added @extend_schema throughout — this file previously had none, so
  everything below rendered as bare/untyped in /api/docs/.
'''

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from kolliq.permissions import IsAuthenticatedOrInternalSecret, resolve_user
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db import transaction as db_transaction
from kolliq.utils import success_response, error_response
from .models import Job, JobApplication, Rating
from .serializers import (
    JobCreateSerializer, JobListSerializer, JobDetailSerializer,
    JobApplicationSerializer, RatingCreateSerializer, RatingListSerializer
)
from .matching import match_jobs_for_worker
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


class JobFeedView(APIView):
    """Worker-only: personalized, match-scored job feed."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='jobs_feed',
        summary='Get matched job feed (worker only)',
        description=(
            'Returns jobs ranked by match score for the authenticated worker — location, '
            'skills, and other factors feed the ranking. Worker-role accounts only; returns '
            '403 for employer/trader/admin accounts.'
        ),
        request=None,
        responses={
            200: OpenApiResponse(description='Ranked job matches.'),
            401: OpenApiResponse(description='Not authenticated.'),
            403: OpenApiResponse(description='Account is not a worker role.'),
        },
        examples=[
            OpenApiExample(
                'Response',
                value={
                    'jobs': [{
                        'id': 'abc123', 'title': 'Market cleanup', 'pay_per_worker': '5000.00',
                        'match_score': 87, 'distance_km': 2.4, 'employer_rating': 4.8,
                        'score_breakdown': {'location': 40, 'skills': 30, 'rating': 17},
                    }],
                    'count': 1,
                },
                response_only=True,
            ),
        ],
        tags=['Jobs'],
    )
    def get(self, request):
        user = request.user
        if user.role != 'worker':
            return error_response('Job feed is for workers only.', status=403)

        matches = match_jobs_for_worker(user)
        if not matches:
            return success_response({'jobs': [], 'message': 'No matching jobs right now. Check back soon!'})

        jobs_data = []
        for match in matches:
            data = JobListSerializer(match['job']).data
            data['match_score'] = match['match_score']
            data['distance_km'] = match['distance_km']
            data['employer_rating'] = match['employer_rating']
            data['score_breakdown'] = match['score_breakdown']
            jobs_data.append(data)

        return success_response({'jobs': jobs_data, 'count': len(jobs_data)})


class JobCreateView(APIView):
    """
    Employer posts a new job. A tiered platform fee is charged upfront
    from wallet balance (₦100 × ceil(pay_per_worker / ₦5,000) per worker),
    separate from the escrow amount for worker pay.
    """
    permission_classes = [IsAuthenticatedOrInternalSecret]

    @extend_schema(
        operation_id='jobs_create',
        summary='Post a new job (employer only)',
        description=(
            'Creates a job in `open` status but it is NOT yet visible to workers. Two upfront '
            'costs are involved and must not be confused:\n\n'
            '1. **Platform creation fee** — charged from wallet immediately on this call '
            '(₦100 × ceil(pay_per_worker / ₦5,000) × workers_needed). This call fails with 400 '
            'if the wallet balance can\'t cover it.\n'
            '2. **Escrow funding** — the actual worker pay, held separately. This call returns '
            '`escrow_instructions` but does NOT fund escrow — call `/jobs/{id}/fund-escrow/` '
            'next. The job stays invisible to workers until escrow is funded.\n\n'
            'employer-role accounts only.'
        ),
        request=JobCreateSerializer,
        responses={
            201: OpenApiResponse(description='Job created — creation fee charged, escrow still needs funding.'),
            400: OpenApiResponse(description='Validation error or insufficient wallet balance for the creation fee.'),
            401: OpenApiResponse(description='Not authenticated.'),
            403: OpenApiResponse(description='Account is not an employer role.'),
            404: OpenApiResponse(description='Wallet not found.'),
        },
        examples=[
            OpenApiExample(
                'Request',
                value={
                    'title': 'Weekend event set-up crew',
                    'description': 'Chairs and tables, 3 hour job.',
                    'skill_required': 'delivery',
                    'workers_needed': 2,
                    'location_area': 'Lekki Phase 1',
                    'location_city': 'Lagos',
                    'pay_per_worker': '6000.00',
                    'duration_hours': '3.0',
                },
                request_only=True,
            ),
            OpenApiExample(
                'Response',
                value={
                    'job_id': 'abc123',
                    'title': 'Weekend event set-up crew',
                    'pay_per_worker': '6000.00',
                    'workers_needed': 2,
                    'total_escrow_amount': 12000.0,
                    'creation_fee_charged': '200.00',
                    'wallet_balance_after_fee': '4800.00',
                    'escrow_instructions': {'reference': 'ABCDEF123456', 'amount_due': '12000.00'},
                    'message': 'Job posted! Platform fee of ₦200.00 charged. Now transfer ₦12,000 to activate matching. Reference: ABCDEF123456',
                },
                response_only=True,
            ),
        ],
        tags=['Jobs'],
    )
    def post(self, request):
        user, err = resolve_user(request)
        if err:
            return err
        if user.role != 'employer':
            return error_response('Only employers can post jobs.', status=403)

        serializer = JobCreateSerializer(data=request.data, context={'request': request, 'user': user})
        if not serializer.is_valid():
            return error_response(serializer.errors)

        from services.job_fees import calculate_total_job_creation_fee, charge_job_creation_fee

        pay_per_worker = serializer.validated_data['pay_per_worker']
        workers_needed = serializer.validated_data['workers_needed']
        creation_fee = calculate_total_job_creation_fee(pay_per_worker, workers_needed)

        wallet = getattr(user, 'wallet', None)
        if not wallet:
            return error_response('Wallet not found. Cannot create job.', status=404)

        if wallet.balance < creation_fee:
            return error_response(
                f'Insufficient wallet balance for job creation fee. '
                f'Need ₦{creation_fee:,.2f}, have ₦{wallet.balance:,.2f}. Top up your wallet first.',
                status=400,
            )

        with db_transaction.atomic():
            job = serializer.save()
            try:
                charge_job_creation_fee(
                    employer_wallet_id=str(wallet.id),
                    job_id=str(job.id),
                    fee_amount=creation_fee,
                )
            except ValueError as e:
                return error_response(str(e), status=400)

        from services.escrow import get_escrow_payment_instructions
        escrow_info = get_escrow_payment_instructions(job)
        wallet.refresh_from_db()

        return success_response({
            'job_id': str(job.id),
            'title': job.title,
            'pay_per_worker': str(job.pay_per_worker),
            'workers_needed': job.workers_needed,
            'total_escrow_amount': float(job.pay_per_worker) * job.workers_needed,
            'creation_fee_charged': str(creation_fee),
            'wallet_balance_after_fee': str(wallet.balance),
            'escrow_instructions': escrow_info,
            'message': (
                f'Job posted! Platform fee of ₦{creation_fee:,.2f} charged. '
                f"Now transfer ₦{float(job.pay_per_worker) * job.workers_needed:,.0f} "
                f'to activate matching. Reference: {job.escrow_reference}'
            ),
        }, status=201)


class JobAcceptView(APIView):
    """Worker accepts an open, escrow-funded job."""
    permission_classes = [IsAuthenticatedOrInternalSecret]

    @extend_schema(
        operation_id='jobs_accept',
        summary='Accept a job (worker only)',
        description=(
            'Worker accepts an open job. Fails with 409 if the job isn\'t open, escrow isn\'t '
            'funded yet, or the worker already applied. Once enough workers have accepted to '
            'fill `workers_needed`, the job automatically transitions to `in_progress`. '
            'worker-role accounts only.'
        ),
        request=None,
        responses={
            201: OpenApiResponse(description='Application accepted.'),
            400: OpenApiResponse(description='job_id missing.'),
            401: OpenApiResponse(description='Not authenticated.'),
            403: OpenApiResponse(description='Account is not a worker role.'),
            404: OpenApiResponse(description='Job not found.'),
            409: OpenApiResponse(description='Job not open, escrow not funded yet, or already applied.'),
        },
        examples=[
            OpenApiExample('Request', value={'job_id': 'abc123'}, request_only=True),
            OpenApiExample(
                'Response',
                value={
                    'application_id': 'def456',
                    'job_title': 'Market cleanup',
                    'pay': '5000.00',
                    'employer_phone': '+2348012345678',
                    'employer_email': 'chidi.employer@kolliq.app',
                    'location': 'Yaba Market',
                    'message': 'Job accepted! Contact employer to confirm start.',
                },
                response_only=True,
            ),
        ],
        tags=['Jobs'],
    )
    def post(self, request):
        user, err = resolve_user(request)
        if err:
            return err
        if user.role != 'worker':
            return error_response('Only workers can accept jobs.', status=403)

        job_id = request.data.get('job_id')
        if not job_id:
            return error_response('job_id is required.')

        job = get_object_or_404(Job, id=job_id)
        if job.status != Job.Status.OPEN:
            return error_response('This job is no longer available.', status=409)
        if not job.escrow_funded:
            return error_response('This job has not been funded yet.', status=409)
        if JobApplication.objects.filter(job=job, worker=user).exists():
            return error_response('You have already applied to this job.', status=409)

        with db_transaction.atomic():
            application = JobApplication.objects.create(job=job, worker=user, status=JobApplication.Status.ACCEPTED)
            accepted_count = job.applications.filter(status=JobApplication.Status.ACCEPTED).count()
            if accepted_count >= job.workers_needed:
                job.status = Job.Status.IN_PROGRESS
                job.save(update_fields=['status', 'updated_at'])

        from apps.jobs.tasks import notify_employer_worker_accepted
        notify_employer_worker_accepted.delay(str(application.id))

        # phone is optional post phone->email migration — an employer who
        # registered with only an email would otherwise leave the worker
        # with employer_phone: null and no way to reach them. Always include
        # email too so the frontend has a fallback contact method.
        return success_response({
            'application_id': str(application.id),
            'job_title': job.title,
            'pay': str(job.pay_per_worker),
            'employer_phone': job.employer.phone,
            'employer_email': job.employer.email,
            'location': job.location_area,
            'message': 'Job accepted! Contact employer to confirm start.',
        }, status=201)


class JobCompleteView(APIView):
    """Employer confirms job completion, releasing escrow to worker(s)."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='jobs_complete',
        summary='Confirm job completion (employer only)',
        description=(
            'Marks accepted worker applications as `completed` and releases their held escrow '
            'payment. Pass `worker_id` to complete for a single worker only (partial completion '
            'on multi-worker jobs); omit it to complete for all accepted workers at once and '
            'mark the job itself `completed`. employer-role accounts only, and only for jobs '
            'they own.'
        ),
        request=None,
        responses={
            200: OpenApiResponse(description='Job completed, escrow release triggered.'),
            400: OpenApiResponse(description='job_id missing or no accepted workers found.'),
            401: OpenApiResponse(description='Not authenticated.'),
            403: OpenApiResponse(description='Account is not an employer role.'),
            404: OpenApiResponse(description='Job not found or not owned by this employer.'),
            409: OpenApiResponse(description='Job is in a status that cannot be completed.'),
        },
        examples=[
            OpenApiExample('Request (all workers)', value={'job_id': 'abc123'}, request_only=True),
            OpenApiExample('Request (single worker)', value={'job_id': 'abc123', 'worker_id': 'xyz789'}, request_only=True),
            OpenApiExample(
                'Response',
                value={'job_id': 'abc123', 'status': 'completed', 'workers_paid': 2, 'message': 'Job confirmed. Payments released.'},
                response_only=True,
            ),
        ],
        tags=['Jobs'],
    )
    def post(self, request):
        user = request.user
        if user.role != 'employer':
            return error_response('Only employers can confirm job completion.', status=403)

        job_id = request.data.get('job_id')
        if not job_id:
            return error_response('job_id is required.')

        job = get_object_or_404(Job, id=job_id, employer=user)
        if job.status not in [Job.Status.OPEN, Job.Status.IN_PROGRESS, Job.Status.FILLED]:
            return error_response(f'Job cannot be completed from status: {job.status}', status=409)

        worker_id = request.data.get('worker_id')

        with db_transaction.atomic():
            applications = (job.applications.filter(worker_id=worker_id, status=JobApplication.Status.ACCEPTED)
                             if worker_id else job.applications.filter(status=JobApplication.Status.ACCEPTED))
            if not applications.exists():
                return error_response('No accepted workers found for this job.')

            worker_ids = list(applications.values_list('worker_id', flat=True))
            applications.update(status=JobApplication.Status.COMPLETED, completed_at=timezone.now())
            job.status = Job.Status.COMPLETED
            job.save(update_fields=['status', 'updated_at'])

            from apps.payments.tasks import release_escrow_for_job
            for wid in worker_ids:
                release_escrow_for_job.delay(str(job.id), str(wid))

        return success_response({
            'job_id': str(job.id), 'status': 'completed',
            'workers_paid': len(worker_ids), 'message': 'Job confirmed. Payments released.',
        })


class JobDetailView(APIView):
    """Get a single job's full details."""
    permission_classes = [IsAuthenticatedOrInternalSecret]

    @extend_schema(
        operation_id='jobs_retrieve',
        summary='Get job details',
        description='Get full details for a single job by ID.',
        request=None,
        responses={
            200: OpenApiResponse(response=JobDetailSerializer, description='Job details.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Job not found.'),
        },
        tags=['Jobs'],
    )
    def get(self, request, job_id):
        job = get_object_or_404(Job, id=job_id)
        return success_response(JobDetailSerializer(job).data)


class MyJobsView(APIView):
    """Employer: jobs posted. Worker: applications made. Response shape differs by role."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='jobs_mine_list',
        summary='Get my jobs or applications',
        description=(
            'Response shape depends on the caller\'s role: employers get `jobs` (jobs they '
            'posted), everyone else gets `applications` (jobs they applied to). Check the '
            'response key present, not just the account role, to be safe.'
        ),
        request=None,
        responses={
            200: OpenApiResponse(description='Jobs (employer) or applications (worker), depending on role.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        examples=[
            OpenApiExample(
                'Employer response',
                value={'jobs': [{'id': 'abc123', 'title': 'Market cleanup', 'status': 'open'}], 'count': 1},
                response_only=True,
            ),
            OpenApiExample(
                'Worker response',
                value={'applications': [{'id': 'def456', 'job': {'title': 'Market cleanup'}, 'status': 'accepted'}], 'count': 1},
                response_only=True,
            ),
        ],
        tags=['Jobs'],
    )
    def get(self, request):
        user = request.user
        if user.role == 'employer':
            jobs = Job.objects.filter(employer=user).order_by('-created_at')
            return success_response({'jobs': JobListSerializer(jobs, many=True).data, 'count': jobs.count()})
        applications = JobApplication.objects.filter(worker=user).select_related('job', 'job__employer').order_by('-accepted_at')
        return success_response({'applications': JobApplicationSerializer(applications, many=True).data, 'count': applications.count()})


class RatingCreateView(APIView):
    """Submit a 1-5 star rating for a counterparty after a job."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='ratings_create',
        summary='Submit a rating',
        description='Rate a counterparty (worker or employer) after a completed job. Triggers a score recalculation for the rated user.',
        request=RatingCreateSerializer,
        responses={
            201: OpenApiResponse(description='Rating submitted.'),
            400: OpenApiResponse(description='Validation error — e.g. already rated this user for this job, or job not completed.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        examples=[
            OpenApiExample('Request', value={'to_user': 'xyz789', 'job': 'abc123', 'stars': 5, 'comment': 'Great work, on time.'}, request_only=True),
        ],
        tags=['Jobs'],
    )
    def post(self, request):
        serializer = RatingCreateSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return error_response(serializer.errors)
        rating = serializer.save()
        from apps.scoring.tasks import recalculate_score
        recalculate_score.delay(str(rating.to_user_id))
        return success_response({'rating_id': str(rating.id), 'stars': rating.stars, 'message': 'Rating submitted. Thank you!'}, status=201)


class UserRatingsView(APIView):
    """Public rating history and average for any user."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='ratings_for_user',
        summary='Get ratings for a user',
        description='Get average rating and full rating history for any user by ID — used to show a worker or employer\'s track record before accepting/posting a job with them.',
        request=None,
        responses={
            200: OpenApiResponse(response=RatingListSerializer, description='Average rating and history.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        examples=[
            OpenApiExample(
                'Response',
                value={'average_rating': 4.8, 'total_ratings': 12, 'ratings': [{'stars': 5, 'comment': 'Great work', 'from_user': 'abc'}]},
                response_only=True,
            ),
        ],
        tags=['Jobs'],
    )
    def get(self, request, user_id):
        ratings = Rating.objects.filter(to_user_id=user_id).order_by('-created_at')
        from django.db.models import Avg
        avg = ratings.aggregate(avg=Avg('stars'))['avg']
        return success_response({
            'average_rating': round(avg, 2) if avg else None,
            'total_ratings': ratings.count(),
            'ratings': RatingListSerializer(ratings, many=True).data,
        })


class JobEscrowInstructionsView(APIView):
    """Employer: get funding instructions/status for a job's escrow."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='jobs_escrow_instructions',
        summary='Get escrow funding instructions (employer only)',
        description='Returns escrow funding instructions for a job the caller owns, or confirms escrow is already funded and the job is live.',
        request=None,
        responses={
            200: OpenApiResponse(description='Escrow status and, if unfunded, funding instructions.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Job not found or not owned by this employer.'),
        },
        tags=['Jobs'],
    )
    def get(self, request, job_id):
        job = get_object_or_404(Job, id=job_id, employer=request.user)
        if job.escrow_funded:
            return success_response({'escrow_funded': True, 'message': 'Escrow already funded. Job is live.', 'job_status': job.status})

        from services.escrow import get_escrow_payment_instructions
        instructions = get_escrow_payment_instructions(job)
        return success_response({'escrow_funded': False, 'job_id': str(job.id), 'job_title': job.title, **instructions})


class JobApplicantsView(APIView):
    """Employer: view accepted/completed applicants for a job they posted."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='jobs_applicants_list',
        summary='Get job applicants (employer only)',
        description='Returns accepted and completed applicants for a job the caller owns, including basic worker profile info for contact purposes.',
        request=None,
        responses={
            200: OpenApiResponse(description='List of applicants with worker details.'),
            401: OpenApiResponse(description='Not authenticated.'),
            403: OpenApiResponse(description='Account is not an employer role.'),
            404: OpenApiResponse(description='Job not found or not owned by this employer.'),
        },
        examples=[
            OpenApiExample(
                'Response',
                value={
                    'job_id': 'abc123', 'job_title': 'Market cleanup', 'workers_needed': 2, 'accepted_count': 1,
                    'applicants': [{
                        'application_id': 'def456', 'status': 'accepted',
                        'worker': {
                            'id': 'xyz789', 'name': 'Amaka Worker', 'phone': '+2348012345678',
                            'email': 'amaka.worker@kolliq.app', 'skills': ['cleaning'],
                        },
                    }],
                },
                response_only=True,
            ),
        ],
        tags=['Jobs'],
    )
    def get(self, request, job_id):
        user = request.user
        if user.role != 'employer':
            return error_response('Only employers can view applicants.', status=403)

        job = get_object_or_404(Job, id=job_id, employer=user)
        applications = job.applications.select_related('worker').filter(
            status__in=[JobApplication.Status.ACCEPTED, JobApplication.Status.COMPLETED]
        ).order_by('-accepted_at')

        data = []
        for app in applications:
            worker = app.worker
            data.append({
                'application_id': str(app.id), 'status': app.status,
                'accepted_at': app.accepted_at, 'completed_at': app.completed_at,
                'worker': {
                    # phone is optional post phone->email migration — name
                    # falls back to phone, but a phone-less worker previously
                    # left the employer with a null phone and no other way to
                    # reach them. email is now always included as a fallback
                    # contact method alongside phone.
                    'id': str(worker.id), 'name': worker.full_name or worker.email or worker.phone,
                    'phone': worker.phone, 'email': worker.email, 'skills': worker.skills,
                    'location': worker.location_area, 'has_vehicle': worker.has_vehicle,
                    'vehicle_type': worker.vehicle_type,
                }
            })

        return success_response({
            'job_id': str(job.id), 'job_title': job.title,
            'workers_needed': job.workers_needed, 'accepted_count': len(data), 'applicants': data,
        })


class JobFundEscrowView(APIView):
    """Employer: fund a job's escrow from wallet balance, making it live."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='jobs_fund_escrow',
        summary='Fund job escrow from wallet (employer only)',
        description=(
            'Debits `pay_per_worker × workers_needed` from the employer\'s wallet into escrow. '
            'This is the step that makes a job actually visible/live to workers — a job created '
            'via `/jobs/` but not yet escrow-funded will not appear in worker feeds. Fails with '
            '409 if escrow is already funded, 400 if wallet balance is insufficient.'
        ),
        request=None,
        responses={
            200: OpenApiResponse(description='Escrow funded, job is now live.'),
            400: OpenApiResponse(description='Insufficient wallet balance.'),
            401: OpenApiResponse(description='Not authenticated.'),
            403: OpenApiResponse(description='Account is not an employer role.'),
            404: OpenApiResponse(description='Job not found, not owned by this employer, or wallet missing.'),
            409: OpenApiResponse(description='Escrow already funded.'),
            500: OpenApiResponse(description='Escrow funding failed unexpectedly — safe to retry.'),
        },
        examples=[
            OpenApiExample(
                'Response',
                value={
                    'job_id': 'abc123', 'job_title': 'Market cleanup', 'escrow_funded': True,
                    'amount_held': '10000.00', 'wallet_balance': '2000.00',
                    'message': '₦10,000.00 held in escrow. Job is now live and workers are being notified.',
                },
                response_only=True,
            ),
        ],
        tags=['Jobs'],
    )
    def post(self, request, job_id):
        user = request.user
        if user.role != 'employer':
            return error_response('Only employers can fund escrow.', status=403)

        job = get_object_or_404(Job, id=job_id, employer=user)
        if job.escrow_funded:
            return error_response('Escrow already funded. Job is live.', status=409)

        wallet = getattr(user, 'wallet', None)
        if not wallet:
            return error_response('Wallet not found.', status=404)

        total_amount = (job.pay_per_worker * job.workers_needed).quantize(Decimal('0.01'))
        if wallet.balance < total_amount:
            return error_response(
                f'Insufficient wallet balance. Need ₦{total_amount:,.2f}, have ₦{wallet.balance:,.2f}. Top up first.',
                status=400,
            )

        try:
            with db_transaction.atomic():
                wallet.debit(total_amount)
                wallet.escrow_balance += total_amount
                wallet.save(update_fields=['escrow_balance', 'updated_at'])

                job.escrow_funded = True
                if not job.escrow_reference:
                    job.escrow_reference = str(job.id).replace('-', '')[:12].upper()
                job.save(update_fields=['escrow_funded', 'escrow_reference', 'updated_at'])

                from apps.payments.models import Transaction
                Transaction.objects.create(
                    user=user, transaction_type=Transaction.Type.ESCROW_HOLD,
                    amount=total_amount, status=Transaction.Status.SUCCESS, job=job,
                    description=f'Escrow funded from wallet for: {job.title}',
                    metadata={'job_id': str(job.id), 'pay_per_worker': str(job.pay_per_worker), 'workers_needed': job.workers_needed, 'funded_from': 'wallet'},
                )
        except ValueError as e:
            return error_response(str(e), status=400)
        except Exception as e:
            logger.error(f"Fund escrow failed: job={job_id} user={user.id} error={e}")
            return error_response('Failed to fund escrow. Please try again.', status=500)

        from apps.jobs.tasks import trigger_job_matching_notifications
        trigger_job_matching_notifications.delay(str(job.id))

        return success_response({
            'job_id': str(job.id), 'job_title': job.title, 'escrow_funded': True,
            'amount_held': str(total_amount), 'wallet_balance': str(wallet.balance),
            'message': f'₦{total_amount:,.2f} held in escrow. Job is now live and workers are being notified.',
        })