"""
Run this ONCE to generate your VAPID keys.

    python generate_vapid_keys.py

It will:
  1. Create a file called vapid_private_key.pem  (KEEP THIS SECRET, server only)
  2. Print a VAPID_PUBLIC_KEY value to put in your .env

Requires: pip install py-vapid cryptography --break-system-packages
"""

import base64
from py_vapid import Vapid01
from cryptography.hazmat.primitives import serialization

vapid = Vapid01()
vapid.generate_keys()

# Save private key as a PEM file - pywebpush reads this file directly
vapid.save_key("vapid_private_key.pem")

# Public key must be sent to the browser as URL-safe base64 (no padding)
public_bytes = vapid.public_key.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint
)
public_key_b64 = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode()

print("\n========== VAPID SETUP ==========\n")
print("1) vapid_private_key.pem has been created in this folder.")
print("   Move it next to app.py (do NOT commit it to git).\n")
print("2) Add these lines to your .env file:\n")
print(f"VAPID_PUBLIC_KEY={public_key_b64}")
print("VAPID_PRIVATE_KEY_FILE=vapid_private_key.pem")
print("VAPID_CLAIM_EMAIL=mailto:you@example.com")
print("\n==================================\n")
