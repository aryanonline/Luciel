"""Pydantic schemas for Arc 10 lifecycle endpoints.

Request and response models for:
  POST  /admin/account/close
  POST  /admin/account/reactivate/stage
  POST  /admin/account/reactivate/complete
  POST  /admin/account/export
  GET   /admin/account/export/{job_id}
  GET   /billing/downgrade/grace
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------
# Closure.
# ---------------------------------------------------------------------

class AccountCloseRequest(BaseModel):
    """POST /admin/account/close body.

    cancel_mode:
      'immediate'  -- cancel Stripe sub immediately, no proration.
                      Customer loses entitlements at once but the
                      30-day grace window for reactivation starts.
      'period_end' -- cancel at current_period_end. Customer keeps
                      entitlements through what they already paid
                      for; grace window starts now regardless.

    confirm_account_name: the admin's exact account name as a
      type-to-confirm guard. The closure service validates this
      against the admin's record. Mis-typed -> 400.

    request_export: if True, the closure flow enqueues a pre-closure
      data export bundle and returns the job id in the response so
      the frontend can poll for status.
    """

    cancel_mode: Literal["immediate", "period_end"]
    confirm_account_name: str = Field(..., min_length=1, max_length=200)
    request_export: bool = False


class AccountCloseResponse(BaseModel):
    """POST /admin/account/close 200 body."""

    admin_id: str
    closure_initiated_at: datetime
    grace_window_expires_at: datetime
    cancel_mode: Literal["immediate", "period_end"]
    stripe_cancellation_applied: bool
    data_export_job_id: str | None = None


# ---------------------------------------------------------------------
# Reactivation.
# ---------------------------------------------------------------------

class ReactivationStageRequest(BaseModel):
    """POST /admin/account/reactivate/stage body."""

    target_tier: Literal["free", "pro", "enterprise"]
    success_url: str = Field(..., min_length=1, max_length=2048)
    cancel_url: str = Field(..., min_length=1, max_length=2048)


class ReactivationStageResponse(BaseModel):
    """POST /admin/account/reactivate/stage 200 body.

    The frontend redirects to ``stripe_checkout_url``. On
    success_url, it calls /reactivate/complete with the session id.
    """

    admin_id: str
    closure_initiated_at: datetime
    grace_window_expires_at: datetime
    stripe_checkout_url: str
    stripe_checkout_session_id: str


class ReactivationCompleteRequest(BaseModel):
    """POST /admin/account/reactivate/complete body."""

    stripe_checkout_session_id: str = Field(..., min_length=1, max_length=200)


class ReactivationCompleteResponse(BaseModel):
    """POST /admin/account/reactivate/complete 200 body."""

    admin_id: str
    reactivated_at: datetime
    new_subscription_id: str
    instances_restored: int
    api_keys_revoked_count: int   # always 0 (Vision 6.4)
    team_members_restored: int    # always 0 (Vision 6.4)


# ---------------------------------------------------------------------
# Data export.
# ---------------------------------------------------------------------

class DataExportRequest(BaseModel):
    """POST /admin/account/export body.

    This route is used for the standalone export path (admin clicks
    "Download my data" outside the closure modal). The closure modal
    triggers an export via its own POST /admin/account/close with
    request_export=true.
    """
    # Future-proof: leaving the model empty for v1 but typed so a
    # caller cannot accidentally send a body that the route does not
    # validate. We keep ConfigDict to forbid extras for the same
    # reason.
    model_config = ConfigDict(extra="forbid")


class DataExportJobResponse(BaseModel):
    """GET /admin/account/export/{job_id} 200 body when status != ready."""

    id: str
    admin_id: str
    status: Literal["pending", "generating", "ready", "expired", "failed"]
    requested_at: datetime
    tier_at_request: Literal["free", "pro", "enterprise"]
    triggered_by: Literal["admin_request", "grace_window_request"]
    ready_at: datetime | None = None
    signed_url_expires_at: datetime | None = None


class DataExportReadyResponse(BaseModel):
    """GET /admin/account/export/{job_id} 200 body when status == ready.

    signed_url has the bundle. The client follows it.
    """

    id: str
    admin_id: str
    status: Literal["ready"]
    signed_url: str
    signed_url_expires_at: datetime
    bytes_size: int


# ---------------------------------------------------------------------
# Downgrade grace.
# ---------------------------------------------------------------------

class DowngradeGraceStatus(BaseModel):
    """GET /billing/downgrade/grace 200 body.

    The frontend reads this to:
      * decide whether to render the read-only banner;
      * decide whether to gate the "new instance" / "upload" UI
        controls;
      * surface the day-30 enforcement date.
    """

    in_grace: bool
    target_tier: Literal["free", "pro", "enterprise"] | None = None
    initiated_at: datetime | None = None
    expires_at: datetime | None = None
