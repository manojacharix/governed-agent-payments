# SPDX-License-Identifier: Apache-2.0
"""The open verifier (06 §1): checks an evidence bundle without trusting the operator that produced it.

Depends on gap_core only, so the verifier and everything it imports are Apache-2.0.
"""
from gap_verify.verify import verify_bundle

__all__ = ["verify_bundle"]
