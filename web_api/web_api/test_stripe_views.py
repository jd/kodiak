from datetime import timedelta
from typing import Tuple

import pytest
import stripe
from django.utils import timezone
from pytest_mock import MockFixture

from web_api.models import (
    Account,
    AccountMembership,
    AccountType,
    StripeCustomerInformation,
    User,
    UserPullRequestActivity,
)
from web_api.testutils import TestClient as Client


def create_account() -> Tuple[User, Account]:
    user = User.objects.create(
        github_id=10137,
        github_login="ghost",
        github_access_token="33149942-C986-42F8-9A45-AD83D5077776",
    )
    account = Account.objects.create(
        github_installation_id=377930,
        github_account_id=900966,
        github_account_login=user.github_login,
        github_account_type="User",
    )
    AccountMembership.objects.create(account=account, user=user, role="member")
    return (user, account)


def create_stripe_customer_info() -> StripeCustomerInformation:
    return StripeCustomerInformation.objects.create(
        customer_id="cus_H2pvQ2kt7nk0JY",
        subscription_id="sub_Gu1xedsfo1",
        plan_id="plan_G2df31A4G5JzQ",
        customer_email="accounting@acme-corp.com",
        customer_balance=0,
        customer_created=1585781308,
        plan_amount=499,
        subscription_quantity=3,
        subscription_start_date=1585781784,
        subscription_current_period_start=0,
        subscription_current_period_end=100,
    )


def create_active_user(*, account: Account, user_id: int, pr_number: int) -> None:
    repo_name = "acme_web"
    UserPullRequestActivity.objects.create(
        github_installation_id=account.github_installation_id,
        github_repository_name=repo_name,
        github_pull_request_number=pr_number,
        github_user_login=f"acme-user-{user_id}",
        github_user_id=user_id,
        is_private_repository=True,
        activity_date=timezone.now(),
        opened_pull_request=True,
    )
    UserPullRequestActivity.objects.create(
        github_installation_id=account.github_installation_id,
        github_repository_name=repo_name,
        github_pull_request_number=pr_number,
        github_user_login="kodiak[bot]",
        github_user_id=0,
        is_private_repository=True,
        activity_date=timezone.now(),
        opened_pull_request=True,
    )


@pytest.mark.django_db
def test_stripe_self_serve_redirect_view(mocker: MockFixture) -> None:
    """
    Smoke test to ensure the redirect URL for the stripe billing portal works
    in the success case.
    """
    fake_billing_portal_session = stripe.billing_portal.Session.construct_from(
        {
            "created": 1588895592,
            "customer": "cus_HEmOJM4AntPHdz",
            "id": "bps_1GgJWeCoyKa1V9Y6Bfnab1L3",
            "livemode": False,
            "object": "billing_portal.session",
            "return_url": "http://app.localhost.kodiakhq.com:3000/t/134f9ff9-327b-4cb3-a0b3-edf63f23a96e/usage",
            "url": "https://billing.stripe.com/session/O4pTob2jXrlVdYdh1grBH1mXJiOIDgwS",
        },
        "fake-key",
    )

    mocker.patch(
        "web_api.views.stripe.billing_portal.Session.create",
        return_value=fake_billing_portal_session,
    )

    user, account = create_account()

    client = Client()
    client.login(user)
    res = client.get(f"/v1/t/{account.id}/stripe_self_serve_redirect")

    assert res.status_code == 302
    assert res["Location"] == fake_billing_portal_session.url


@pytest.mark.django_db
def test_get_subscription_info_view_valid_account_personal_user(
    mocker: MockFixture,
) -> None:
    """
    all personal accounts have valid subscriptions
    """
    user, account = create_account()
    account.github_account_type = AccountType.user
    account.save()
    create_active_user(account=account, user_id=user.github_id, pr_number=2)

    assert account.github_account_type == AccountType.user

    client = Client()
    client.login(user)
    res = client.get(f"/v1/t/{account.id}/subscription_info")
    assert res.status_code == 200
    assert res.json() == {
        "type": "VALID_SUBSCRIPTION",
    }


@pytest.mark.django_db
def test_get_subscription_info_view_valid_account_trial_user(
    mocker: MockFixture,
) -> None:
    """
    trial users should have a valid subscription
    """
    user, account = create_account()
    account.github_account_type = AccountType.organization
    account.trial_expiration = timezone.now() + timedelta(days=10)
    account.save()

    assert account.github_account_type != AccountType.user
    assert account.active_trial() is True

    client = Client()
    client.login(user)
    res = client.get(f"/v1/t/{account.id}/subscription_info")
    assert res.status_code == 200
    assert res.json() == {
        "type": "VALID_SUBSCRIPTION",
    }


@pytest.mark.django_db
def test_get_subscription_info_view_valid_account_subscription_user(
    mocker: MockFixture,
) -> None:
    """
    valid subscription user, aka they aren't over their subscribed number of seats
    """
    user, account = create_account()
    account.github_account_type = AccountType.organization
    account.trial_expiration = None
    account.save()

    assert account.github_account_type != AccountType.user
    assert account.active_trial() is False
    assert account.trial_expired() is False

    client = Client()
    client.login(user)
    res = client.get(f"/v1/t/{account.id}/subscription_info")
    assert res.status_code == 200
    assert res.json() == {
        "type": "VALID_SUBSCRIPTION",
    }


@pytest.mark.django_db
def test_get_subscription_info_view_trial_expired(mocker: MockFixture) -> None:
    """
    ensure trial expiration is handled explicitly in the response
    """
    user, account = create_account()
    account.github_account_type = AccountType.organization
    account.trial_expiration = timezone.now() - timedelta(days=10)
    account.save()

    assert account.github_account_type != AccountType.user
    assert account.active_trial() is False
    assert account.trial_expired() is True

    client = Client()
    client.login(user)
    res = client.get(f"/v1/t/{account.id}/subscription_info")
    assert res.status_code == 200
    assert res.json() == {
        "type": "TRIAL_EXPIRED",
    }


@pytest.mark.django_db
def test_get_subscription_info_view_subscription_overage(mocker: MockFixture) -> None:
    """
    check the API response when a user's account has exceeded the number of
    seats
    """
    user, account = create_account()
    account.stripe_customer_id = create_stripe_customer_info().customer_id
    account.github_account_type = AccountType.organization
    account.save()
    for i in range(1, 6):
        create_active_user(account=account, user_id=i, pr_number=i)

    assert account.github_account_type != AccountType.user
    assert account.active_trial() is False
    stripe_customer_info = account.get_stripe_customer_info()
    assert stripe_customer_info is not None
    assert stripe_customer_info.subscription_quantity < len(
        UserPullRequestActivity.get_active_users_in_last_30_days(account=account)
    )

    client = Client()
    client.login(user)
    res = client.get(f"/v1/t/{account.id}/subscription_info")
    assert res.status_code == 200
    assert res.json() == {
        "type": "SUBSCRIPTION_OVERAGE",
        "activeUserCount": 5,
        "licenseCount": 3,
    }
