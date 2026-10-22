from collections import Counter
from pathlib import Path
import tempfile
import unittest

from scripts.check_quality import diagnostic_key, new_diagnostics


class QualityGuardTests(unittest.TestCase):
    def test_unchanged_debt_survives_line_shift_but_changed_or_duplicated_debt_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "old.py"
            path.write_text("import unused\n", encoding="utf-8")
            item = {"filename": str(path), "code": "F401", "message": "unused import",
                    "location": {"row": 1}, "end_location": {"row": 1}}
            baseline = Counter({diagnostic_key(item, root): 1})
            path.write_text("# clean line\nimport unused\n", encoding="utf-8")
            moved = {**item, "location": {"row": 2}, "end_location": {"row": 2}}
            self.assertEqual(new_diagnostics([moved], baseline, root), [])
            self.assertEqual(len(new_diagnostics([moved, moved], baseline, root)), 1)
            path.write_text("# clean line\nimport another_unused\n", encoding="utf-8")
            self.assertEqual(new_diagnostics([moved], baseline, root), [moved])

    def test_old_rule_in_new_file_is_not_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old.py", root / "new.py"
            old.write_text("import unused\n", encoding="utf-8")
            new.write_text("import unused\n", encoding="utf-8")
            item = {"filename": str(old), "code": "F401", "message": "unused import",
                    "location": {"row": 1}, "end_location": {"row": 1}}
            baseline = Counter({diagnostic_key(item, root): 1})
            changed = {**item, "filename": str(new)}
            self.assertEqual(new_diagnostics([changed], baseline, root), [changed])
