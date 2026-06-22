from src.middleware.auth import authenticate_user


def test_authenticate_valid_credentials():
    assert authenticate_user("user123", "password123")


def test_authenticate_invalid_password():
    assert not authenticate_user("user123", "wrongpassword")


def test_authenticate_nonexistent_user():
    assert not authenticate_user("nonexistentuser", "password123")


def test_authenticate_empty_username():
    assert not authenticate_user("", "password123")


def test_authenticate_empty_password():
    assert not authenticate_user("user123", "")


def test_authenticate_whitespace_username():
    assert not authenticate_user(" ", "password123")


def test_authenticate_whitespace_password():
    assert not authenticate_user("user123", " ")
