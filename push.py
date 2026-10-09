"""
push.py
--------
Production Ready Web Push Sender
"""

import os
import json
import traceback
from pywebpush import webpush, WebPushException


# ==========================================
# VAPID CONFIG
# ==========================================

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")

VAPID_PRIVATE_KEY_FILE = os.getenv(
    "VAPID_PRIVATE_KEY_FILE",
    "vapid_private_key.pem"
)

VAPID_CLAIM_EMAIL = os.getenv(
    "VAPID_CLAIM_EMAIL",
    "mailto:admin@example.com"
)


# ==========================================
# SEND PUSH
# ==========================================

def send_web_push(
    subscription_info,
    title,
    body,
    url="/customer/dashboard"
):

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url
    })

    print("\n")
    print("=" * 70)
    print("WEB PUSH DEBUG")
    print("=" * 70)

    print("Endpoint:")
    print(subscription_info.get("endpoint"))

    print("\nPrivate Key File:")
    print(VAPID_PRIVATE_KEY_FILE)

    print("\nPrivate Key Exists:")
    print(os.path.exists(VAPID_PRIVATE_KEY_FILE))

    print("\nClaim Email:")
    print(VAPID_CLAIM_EMAIL)

    print("\nPayload:")
    print(payload)

    try:

        response = webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY_FILE,
            vapid_claims={
                "sub": VAPID_CLAIM_EMAIL
            }
        )

        print("\n")
        print("=" * 70)
        print("WEB PUSH SUCCESS")
        print("=" * 70)

        print(response)

        return True, False

    except WebPushException as ex:

        print("\n")
        print("=" * 70)
        print("WEB PUSH FAILED")
        print("=" * 70)

        status = None

        if ex.response is not None:
            status = ex.response.status_code

        print("HTTP Status:")
        print(status)

        print("\nException:")
        print(ex)

        if ex.response is not None:

            try:

                print("\nResponse Headers:")
                print(ex.response.headers)

            except Exception:
                pass

            try:

                print("\nResponse Body:")
                print(ex.response.text)

            except Exception:
                pass

        expired = status in (404, 410)

        print("\nExpired Subscription:")
        print(expired)

        return False, expired

    except Exception as e:

        print("\n")
        print("=" * 70)
        print("UNKNOWN PUSH ERROR")
        print("=" * 70)

        print(type(e).__name__)
        print(e)

        traceback.print_exc()

        return False, False