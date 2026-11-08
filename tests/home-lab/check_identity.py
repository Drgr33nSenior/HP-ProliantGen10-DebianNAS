"""Every host-identity check names the role's declared hostname; no host is touched."""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/roles/debian_microserver"
CHECKS = [
    ("ansible/owner-*.yml", r"ansible_facts\.hostname == '([^']+)'"),
    ("ansible/roles/debian_microserver/files/*.py", r"socket\.gethostname\(\)\.split\('\.'\)\[0\] [!=]= '([^']+)'"),
    ("ansible/roles/debian_microserver/files/*.sh", r"\$\(hostname -s\) == ([A-Za-z0-9-]+)"),
    ("installer/media/preseed.cfg", r"^d-i netcfg/get_hostname string (\S+)$"),
]


class IdentityTests(unittest.TestCase):
    def test_every_identity_check_uses_the_declared_hostname(self):
        expected = yaml.safe_load((ROLE / "defaults/main.yml").read_text())["microserver_hostname"]
        self.assertRegex(expected, r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
        found = []
        for pattern, regex in CHECKS:
            for path in sorted(ROOT.glob(pattern)):
                for name in re.findall(regex, path.read_text(), re.M):
                    found.append((str(path.relative_to(ROOT)), name))
        # Six owner plays, five NAS-side verifiers, the ACME helper and the preseed.
        self.assertGreaterEqual(len(found), 13, found)
        self.assertEqual([item for item in found if item[1] != expected], [])


if __name__ == "__main__":
    unittest.main()
