from relay.security import hash_secret, new_token, verify_secret


def test_hash_and_verify():
    encoded = hash_secret("hunter2", iterations=1000)
    assert encoded.startswith("pbkdf2_sha256$1000$")
    assert verify_secret("hunter2", encoded)
    assert not verify_secret("hunter3", encoded)
    assert not verify_secret("hunter2", None)
    assert not verify_secret("hunter2", "garbage")


def test_hashes_are_salted():
    assert hash_secret("same") != hash_secret("same")


def test_new_token_is_urlsafe_and_unique():
    a, b = new_token(), new_token()
    assert a != b
    assert len(a) > 20
