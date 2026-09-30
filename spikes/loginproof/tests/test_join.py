"""Join logic against fixtures shaped like the documented WorkOS objects (see NOTES.md for field sources)."""

from loginproof import join, report


def login(provider, idp_id, email, raw=None, ts="2026-10-01T10:00:00+00:00"):
    return {
        "ts": ts,
        "provider": provider,
        "profile_id": "prof_01",
        "idp_id": idp_id,
        "connection_id": "conn_01",
        "connection_type": provider,
        "email": email,
        "groups": [],
        "raw_attribute_keys": sorted((raw or {}).keys()),
        "raw_attributes": raw or {},
    }


def duser(uid, idp_id, emails, groups, raw=None, state="active"):
    return {
        "id": uid,
        "idp_id": idp_id,
        "username": emails[0],
        "state": state,
        "emails": emails,
        "groups": groups,
        "group_idp_ids": [],
        "raw_attributes": raw or {},
    }


OKTA_DIR = {"users": [duser("du_1", "00u1abc", ["ann@example.test"], ["Finance", "Admins"]), duser("du_2", "00u2def", ["bob@example.test"], ["Finance"])]}
ENTRA_DIR = {
    "users": [
        duser(
            "du_e1",
            "8f1c2d3e-0000-0000-0000-000000000001",
            ["ann@example.test"],
            ["Finance"],
            raw={"externalId": "8f1c2d3e-0000-0000-0000-000000000001", "userName": "ann@example.test"},
        )
    ]
}
GOOGLE_DIR = {"users": [duser("du_g1", "1180000000000001", ["ann@example.test"], ["Finance"])]}


def test_okta_joins_on_idp_id_and_survives_email_change():
    records = {
        "logins": [
            login("OKTA", "00u1abc", "ann@example.test"),
            login("OKTA", "00u1abc", "ann.smith@example.test", ts="2026-10-01T11:00:00+00:00"),
        ],
        "directories": {"OKTA": OKTA_DIR},
        "device": {},
    }
    r = join.evaluate(records)["OKTA"]
    assert r["join_key"] == "idp_id"
    assert r["matched"] == 2
    assert r["stable_across_email_change"] is True
    assert r["group_sharing_possible"] is True


def test_entra_saml_upn_nameid_joins_through_objectidentifier_claim():
    raw = {
        "http://schemas.microsoft.com/identity/claims/objectidentifier": "8f1c2d3e-0000-0000-0000-000000000001",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name": "ann@example.test",
    }
    records = {"logins": [login("ENTRA_SAML", "ann@example.test", "ann@example.test", raw)], "directories": {"ENTRA": ENTRA_DIR}, "device": {}}
    r = join.evaluate(records)["ENTRA_SAML"]
    assert r["join_key"].startswith("login.raw_attributes.")
    assert r["join_key"].endswith("== directory.idp_id")
    assert r["group_sharing_possible"] is True


def test_entra_oidc_oid_claim_joins_directory_objectid():
    raw = {"oid": "8f1c2d3e-0000-0000-0000-000000000001", "sub": "pairwise-sub-value"}
    records = {"logins": [login("ENTRA_OIDC", "pairwise-sub-value", "ann@example.test", raw)], "directories": {"ENTRA": ENTRA_DIR}, "device": {}}
    r = join.evaluate(records)["ENTRA_OIDC"]
    assert r["join_key"] == "login.raw_attributes.oid == directory.idp_id"


def test_google_saml_without_id_claim_falls_back_to_email_and_is_not_stable():
    records = {
        "logins": [
            login("GOOGLE", None, "ann@example.test"),
            login("GOOGLE", None, "ann.smith@example.test", ts="2026-10-01T11:00:00+00:00"),
        ],
        "directories": {"GOOGLE": GOOGLE_DIR},
        "device": {},
    }
    r = join.evaluate(records)["GOOGLE"]
    assert r["idp_id_present"] is False
    assert r["matched"] == 1
    assert r["join_key"] == "email"
    assert r["group_sharing_possible"] is False


def test_new_idp_id_after_email_change_is_reported_unstable():
    users = [duser("du_1", "x", ["ann@example.test", "ann.smith@example.test"], ["Finance"])]
    records = {
        "logins": [login("OKTA", "id-before", "ann@example.test"), login("OKTA", "id-after", "ann.smith@example.test")],
        "directories": {"OKTA": {"users": users}},
        "device": {},
    }
    r = join.evaluate(records)["OKTA"]
    assert r["join_key"] == "email"
    assert r["stable_across_email_change"] is False


def test_unmatched_login_has_no_key():
    records = {"logins": [login("OKTA", "unknown", "zed@example.test")], "directories": {"OKTA": OKTA_DIR}, "device": {}}
    r = join.evaluate(records)["OKTA"]
    assert r["matched"] == 0
    assert r["join_key"] is None
    assert r["stable_across_email_change"] == join.NOT_MEASURED


def test_report_renders_before_any_run_and_after_device_recheck():
    empty = report.render({"logins": [], "directories": {}, "device": {}})
    assert empty.count("not measured") >= 12
    done = report.render(
        {
            "logins": [login("OKTA", "00u1abc", "ann@example.test")],
            "directories": {"OKTA": OKTA_DIR},
            "device": {"flow": {"offered": True, "user_id": "user_01"}, "recheck": {"refresh_http": 400, "refresh_error": "invalid_grant", "refresh_succeeded": False, "token_revoked_on_deactivation": True}},
        }
    )
    assert "| OKTA | 1 | 2 | idp_id | not measured | yes | yes |" in done
    assert "Fallback design" in done


def test_login_before_a_directory_email_change_still_joins_on_idp_id_and_is_stable():
    users = [duser("du_1", "00u1abc", ["ann.smith@example.test"], ["Finance"])]
    records = {
        "logins": [login("OKTA", "00u1abc", "ann@example.test")],
        "directories": {"OKTA": {"users": users}},
        "device": {},
    }
    r = join.evaluate(records)["OKTA"]
    assert r["join_key"] == "idp_id"
    assert r["stable_across_email_change"] is True


def test_google_saml_idp_id_that_is_the_email_is_not_stable():
    records = {
        "logins": [login("GOOGLE", "ann@example.test", "ann@example.test")],
        "directories": {"GOOGLE": GOOGLE_DIR},
        "device": {},
    }
    r = join.evaluate(records)["GOOGLE"]
    assert r["join_key"] == "email"
    assert r["stable_across_email_change"] is False


def test_directory_without_groups_leaves_group_sharing_unmeasured():
    no_groups = {"users": [{**ENTRA_DIR["users"][0], "groups": []}]}
    login_rec = login("ENTRA_OIDC", "8f1c2d3e-0000-0000-0000-000000000001", "ann@example.test")
    records = {"logins": [login_rec], "directories": {"ENTRA": no_groups}, "device": {}}
    r = join.evaluate(records)["ENTRA_OIDC"]
    assert r["join_key"] == "idp_id"
    assert r["group_sharing_possible"] == join.NOT_MEASURED
