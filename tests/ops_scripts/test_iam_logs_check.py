import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "ops"))
import iam_logs_check as ic  # noqa: E402

ME = "github-deployer@proj.iam.gserviceaccount.com"


def test_resumen_sin_correos():
    pol = {"bindings": [
        {"role": "roles/run.admin", "members": [f"serviceAccount:{ME}"]},
        {"role": "roles/logging.logWriter", "members": [f"serviceAccount:{ME}"],
         "condition": {"expression": "x"}},
        {"role": "roles/logging.logWriter", "members": ["serviceAccount:github-deployer@otro.iam.gserviceaccount.com",
                                                        f"deleted:serviceAccount:{ME}?uid=1"]},
    ]}
    out = "\n".join(ic.policy_summary(pol, ME))
    assert "@" not in out
    assert "['roles/run.admin']" in out
    assert "escribir logs?: no (hay uno con condición" in out
    assert "OTRO proyecto con roles aquí: ['roles/logging.logWriter']" in out
    assert "BORRADA (deleted:) con roles aquí: ['roles/logging.logWriter']" in out


def test_motivo_sin_mensaje():
    r = {"error": {"status": "PERMISSION_DENIED", "message": "a@b.com no puede",
                   "details": [{"reason": "SERVICE_DISABLED"}]}}
    assert ic.why(r) == "PERMISSION_DENIED (motivo: SERVICE_DISABLED)"
