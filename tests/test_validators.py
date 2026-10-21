from app.validators import password_problems, valid_email


def test_valid_emails():
    for e in ["a@b.co", "pat.kumar+tag@sub.example.com", "x_y@my-site.org"]:
        assert valid_email(e), e


def test_invalid_emails():
    for e in ["bad@", "a@b", "a b@c.com", "a..b@c.com", "a@-b.com", "x@y.c", "ok@exa_mple.com", "@x.com", "a@b.123", "a@@b.com", "", "x" * 300 + "@a.com"]:
        assert not valid_email(e), e


def test_password_rules():
    assert password_problems("abc")
    assert not password_problems("Sup3rSecretPass", "pat@x.com")
    assert "not contain your email name" in password_problems("patkumar123456", "patkumar@x.com")
