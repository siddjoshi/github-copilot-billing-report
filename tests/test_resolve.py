from copilot_aic_report.models import IdentityMapEntry
from copilot_aic_report.resolve import (
    IdentityResolver,
    Resolution,
    canonicalize_login,
    emu_shortname,
    is_obfuscated_login,
    is_placeholder_login,
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


def test_is_placeholder_login_detects_guid():
    assert is_placeholder_login("2f1c8e4a-1234-4abc-9def-0123456789ab")
    assert is_placeholder_login("2f1c8e4a-1234-4abc-9def-0123456789ab_acme")  # with EMU suffix
    assert not is_placeholder_login("mona_acme")
    assert not is_placeholder_login("octocat")
    assert not is_placeholder_login(None)


def test_looks_like_external_id_flags_guid():
    assert looks_like_external_id("2f1c8e4a-1234-4abc-9def-0123456789ab_acme")


def test_resolve_guid_via_external_index():
    r = IdentityResolver(identity_index={"2f1c8e4a-1234-4abc-9def-0123456789ab": "mona_acme"})
    res = r.resolve(external_id="2f1c8e4a-1234-4abc-9def-0123456789ab")
    assert res.user_login == "mona_acme"
    assert res.source == "externalIdentities"


def test_is_obfuscated_login_detects_hex_and_guid():
    # Obfuscated deprovisioned EMU handle: long hex + shortcode suffix.
    assert is_obfuscated_login("4eb6538565c3d97ad2917d606ccdc4_LTIMPG")
    assert is_obfuscated_login("1fa92af3ac5131c93a570316ba3d04_LTIMPG")
    # Bare long hex, no suffix.
    assert is_obfuscated_login("4eb6538565c3d97ad2917d606ccdc4")
    # GUID form is also obfuscated.
    assert is_obfuscated_login("2f1c8e4a-1234-4abc-9def-0123456789ab_acme")
    # Real EMU / plain logins are NOT obfuscated.
    assert not is_obfuscated_login("Hemant-10832601_HondaCN")
    assert not is_obfuscated_login("mona_acme")
    assert not is_obfuscated_login("octocat")
    assert not is_obfuscated_login("dead_beef")  # short hex-ish shortname, real login
    assert not is_obfuscated_login(None)
    assert not is_obfuscated_login("")
