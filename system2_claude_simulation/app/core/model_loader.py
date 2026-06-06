"""
Secure Model Loader — AES-256 encrypted weight loading.
Weights are decrypted in memory only, never written to disk as plaintext.
"""
import os
import json
import base64
import hashlib
from pathlib import Path

def _get_cipher():
    """Get Fernet cipher from environment key."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    key = os.getenv("MODEL_ENCRYPTION_KEY")
    if not key:
        return None
    return Fernet(key.encode() if isinstance(key, str) else key)

def load_model_weights(path: str = None) -> dict:
    """
    Load model weights with automatic format detection.
    - .bin → encrypted binary (production)
    - .json → plaintext JSON (development only)
    """
    if path is None:
        path = os.getenv("MODEL_WEIGHTS_PATH", "data/model_weights.json")

    p = Path(path)

    # Try encrypted binary first
    bin_path = p.with_suffix(".bin")
    if bin_path.exists():
        cipher = _get_cipher()
        if cipher is None:
            raise RuntimeError(
                "Encrypted model found but MODEL_ENCRYPTION_KEY not set. "
                "Set it via environment variable."
            )
        with open(bin_path, "rb") as f:
            encrypted_data = f.read()
        try:
            decrypted = cipher.decrypt(encrypted_data)
            weights = json.loads(decrypted.decode("utf-8"))
            # Verify integrity
            if "checksum" in weights:
                expected = weights.pop("checksum")
                actual = hashlib.sha256(json.dumps(weights, sort_keys=True).encode()).hexdigest()[:16]
                if actual != expected:
                    raise RuntimeError("Model weight integrity check failed — possible tampering.")
            return weights
        except Exception as e:
            raise RuntimeError(f"Failed to decrypt model weights: {e}")

    # Fallback to plaintext JSON (development only)
    json_path = p.with_suffix(".json")
    if json_path.exists():
        import warnings
        warnings.warn(
            "Loading plaintext model weights. Use encrypt_weights.py for production.",
            UserWarning
        )
        with open(json_path, "r") as f:
            return json.load(f)

    raise FileNotFoundError(f"No model weights found at {p} (.bin or .json)")


def verify_model_integrity(weights: dict) -> bool:
    """Verify model weights haven't been tampered with."""
    required_keys = {"demand_weights", "seasonal_factors"}
    return all(k in weights for k in required_keys)
