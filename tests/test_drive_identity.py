"""Publishing as a second service account.

Drive's 750 GB per rolling 24 hours is charged **per identity**, so a night too
big for one service account can be published by two. This covers the plumbing
that makes that work — and the one detail that silently breaks it: the
credentials used to *ask* for an impersonated token need cloud-platform, while
the token itself needs Drive. Get those the wrong way round and it fails at
runtime with a scope error, on the VM, hours in."""
import os, sys, tempfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="identity_")

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
from integrations import drive

TARGET = "video-uploads-2@companion-app-26947.iam.gserviceaccount.com"

asked: dict = {}
built: dict = {}


class _SourceCreds:
    def __init__(self, scopes):
        self.scopes = scopes
        self.service_account_email = "the-vm@companion-app-26947.iam.gserviceaccount.com"


class _FakeGoogleAuth:
    @staticmethod
    def default(scopes=None):
        asked["scopes"] = list(scopes or [])
        return _SourceCreds(scopes), "companion-app-26947"


class _FakeImpersonated:
    def __init__(self, source_credentials=None, target_principal=None,
                 target_scopes=None, lifetime=None):
        built.update(source=source_credentials, target=target_principal,
                     scopes=list(target_scopes or []), lifetime=lifetime)
        self.service_account_email = target_principal


drive._import_google = lambda: (_FakeGoogleAuth, None, None, None)
# _impersonate imports this lazily from google.auth; swap the real class out.
import google.auth.impersonated_credentials as _real_module

_real_class = _real_module.Credentials
_real_module.Credentials = _FakeImpersonated


# ---- no impersonation: unchanged behaviour ---------------------------------
config.DRIVE_IMPERSONATE = ""
config.DRIVE_CREDENTIALS_FILE = ""
creds = drive._credentials()
assert isinstance(creds, _SourceCreds)
assert asked["scopes"] == drive.SCOPES, asked
assert not built, "impersonated with nothing configured"
assert drive.acting_identity().endswith("the-vm@companion-app-26947.iam.gserviceaccount.com")
print("default: the VM's own account, asking for the Drive scope directly")


# ---- impersonating a second account ----------------------------------------
config.DRIVE_IMPERSONATE = TARGET
creds = drive._credentials()

assert isinstance(creds, _FakeImpersonated)
assert built["target"] == TARGET, built
# The source only needs to be allowed to MINT a token; the Drive scope belongs
# to the token it mints. Asking the source for Drive is the mistake this pins.
assert asked["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"], \
    f"the source credentials asked for the wrong scope: {asked['scopes']}"
assert built["scopes"] == drive.SCOPES, \
    f"the impersonated token must carry the Drive scope: {built['scopes']}"
assert built["lifetime"], "a token with no lifetime cannot be short-lived"
print(f"impersonation: source asks for cloud-platform, token carries Drive, "
      f"target {TARGET.split('@')[0]}")

assert drive.acting_identity() == TARGET, \
    "the setup check would name the wrong account as the one being charged"
print("acting_identity: names the impersonated account, not the VM's")


# ---- a key file can be impersonated FROM as well ----------------------------
# The two settings are independent: a key file says who we start as, the
# impersonation target says who we end up as.
key = Path(tempfile.mkdtemp()) / "key.json"
key.write_text("{}", encoding="utf-8")


class _FromFile:
    """Stands in for the `google.oauth2.service_account` MODULE, so the
    attribute path the real code walks (`.Credentials.from_service_account_
    file`) is the one under test."""

    class Credentials:
        @staticmethod
        def from_service_account_file(path, scopes=None):
            asked["file_scopes"] = list(scopes or [])
            return _SourceCreds(scopes)


drive._import_google = lambda: (_FakeGoogleAuth, _FromFile, None, None)
config.DRIVE_CREDENTIALS_FILE = str(key)
built.clear()
creds = drive._credentials()
assert isinstance(creds, _FakeImpersonated) and built["target"] == TARGET
assert asked["file_scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]
print("key file + impersonation: starts as the key, publishes as the target")

# A missing key file is still a clear error rather than a mysterious 403.
config.DRIVE_CREDENTIALS_FILE = str(key.parent / "nope.json")
try:
    drive._credentials()
    raise AssertionError("a missing key file was accepted")
except drive.DriveError as exc:
    assert "not found" in str(exc)
print("missing key file: named plainly")

_real_module.Credentials = _real_class
print("\nALL DRIVE IDENTITY TESTS PASSED")
