from django.urls import path
from .views import (
    JobFeedView, JobCreateView, JobAcceptView, JobCompleteView,
    JobDetailView, MyJobsView, RatingCreateView, UserRatingsView,
    JobEscrowInstructionsView, JobApplicantsView, JobFundEscrowView,
)
 
urlpatterns = [
    path('feed/',                     JobFeedView.as_view(),               name='job-feed'),
    path('create/',                   JobCreateView.as_view(),             name='job-create'),
    path('accept/',                   JobAcceptView.as_view(),             name='job-accept'),
    path('complete/',                 JobCompleteView.as_view(),           name='job-complete'),
    path('mine/',                     MyJobsView.as_view(),                name='my-jobs'),
    path('rate/',                     RatingCreateView.as_view(),          name='rating-create'),
    path('<uuid:job_id>/',            JobDetailView.as_view(),             name='job-detail'),
    path('<uuid:job_id>/escrow/',     JobEscrowInstructionsView.as_view(), name='job-escrow'),
    path('<uuid:job_id>/applicants/', JobApplicantsView.as_view(),         name='job-applicants'),
    path('<uuid:job_id>/fund/',       JobFundEscrowView.as_view(),         name='job-fund-escrow'),
    path('ratings/<uuid:user_id>/',   UserRatingsView.as_view(),           name='user-ratings'),
]
