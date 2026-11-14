"""A software passkey: just enough of an authenticator to exercise the server's WebAuthn checks without a browser."""

import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url as b64u


class SoftKey:
    def __init__(self, rp_id: str = "testserver", origin: str = "http://testserver"):
        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.counter = 0

    @property
    def id(self) -> str:
        return b64u(self.cred_id)

    def _client(self, kind: str, challenge: str, origin: str | None = None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin, "crossOrigin": False}).encode()

    def create(self, options: dict, origin: str | None = None, user_verified: bool = True) -> dict:
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        flags = 0x01 | 0x40 | (0x04 if user_verified else 0)
        auth = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.counter) + bytes(16)
                + struct.pack(">H", len(self.cred_id)) + self.cred_id + cose)
        return {"id": self.id, "rawId": self.id, "type": "public-key", "clientExtensionResults": {},
                "response": {"attestationObject": b64u(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})),
                             "clientDataJSON": b64u(self._client("webauthn.create", options["challenge"], origin)),
                             "transports": ["internal"]}}

    def get(self, options: dict, origin: str | None = None, user_verified: bool = True, counter: int | None = None) -> dict:
        self.counter = counter if counter is not None else self.counter + 1
        flags = 0x01 | (0x04 if user_verified else 0)
        auth = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.counter)
        client = self._client("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": self.id, "rawId": self.id, "type": "public-key", "clientExtensionResults": {},
                "response": {"authenticatorData": b64u(auth), "clientDataJSON": b64u(client), "signature": b64u(sig), "userHandle": None}}
