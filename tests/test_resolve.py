from copilot_aic_report.models import IdentityMapEntry
from copilot_aic_report.resolve import (
    IdentityResolver,
    Resolution,
    canonicalize_login,
    emu_shortname,
    looks_like_external_id,
)


def test_seat_login_wins():
    r = IdentityResolver()
    res = r.resolve(seat_login="mona_acme", audit_login="other", external_id="x@e.com")
    assert res.user_login == "mona_acme"
    assert res.source == "seat"
    assert res.resolved


def test_audit_login_second():
    r = IdentityResolver()
    res = r.resolve(seat_login=None, audit_login="octocat", external_id="x@e.com")
    assert res.user_login == "octocat"
    assert res.source == "audit"


def test_external_identity_index_third():
    r = IdentityResolver(identity_index={"mona@acme.com": "mona_acme"})
    res = r.resolve(external_id="MONA@acme.com")
    assert res.user_login == "mona_acme"
    assert res.source == "externalIdentities"
    assert res.external_identity == "MONA@acme.com"


def test_identity_map_fourth():
    entries = [IdentityMapEntry(github_login="hubot", scim_username="hubot.svc", email="h@e.com")]
    r = IdentityResolver(identity_map=entries)
    res = r.resolve(external_id="hubot.svc")
    assert res.user_login == "hubot"
    assert res.source == "identity_map"


def test_external_index_precedes_map():
    r = IdentityResolver(
        identity_index={"k": "from_index"},
        identity_map=[IdentityMapEntry(github_login="from_map", scim_username="k")],
    )
    res = r.resolve(external_id="k")
    assert res.user_login == "from_index"
    assert res.source == "externalIdentities"


def test_unresolved_never_leaks_external_id():
    r = IdentityResolver()
    res = r.resolve(external_id="ghost@acme.com")
    assert res.user_login is None
    assert res.source == "unresolved"
    assert res.external_identity == "ghost@acme.com"
    assert not res.resolved


def test_identity_map_skips_entries_without_login():
    entries = [IdentityMapEntry(github_login=None, scim_username="orphan")]
    r = IdentityResolver(identity_map=entries)
    res = r.resolve(external_id="orphan")
    assert res.source == "unresolved"


def test_canonicalize_login():
    assert canonicalize_login("@mona_acme") == "mona_acme"
    assert canonicalize_login("  mona  ") == "mona"
    assert canonicalize_login(None) is None
    assert canonicalize_login("") is None
    # EMU suffix preserved
    assert canonicalize_login("mona_acme") == "mona_acme"


def test_emu_shortname():
    assert emu_shortname("mona_acme") == "mona"
    assert emu_shortname("plainlogin") == "plainlogin"
    assert emu_shortname(None) is None


def test_looks_like_external_id():
    assert looks_like_external_id("a@b.com")
    assert looks_like_external_id("First Last")
    assert not looks_like_external_id("mona_acme")
    assert not looks_like_external_id(None)
