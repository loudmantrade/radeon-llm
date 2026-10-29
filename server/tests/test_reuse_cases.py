import os
import unittest
if os.name == 'nt':
    raise unittest.SkipTest('macOS experimental installer and harness')
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from native_reuse_experiment import decode_case


class ReuseCaseTests(unittest.TestCase):
    def test_six_fields_use_defaults(self):
        case=('name','old','new','BLUE','RED','question')
        self.assertEqual(decode_case(case,'default system','default document'),
                         case+('default system','default document'))

    def test_sensitive_case_keeps_both_custom_fields(self):
        case=('context_sensitive_removal','old','new','BLUE','RED','question',
              'custom system','custom document')
        self.assertEqual(decode_case(case,'unused','unused'),case)
        name,old,new,expected,source,question,system,document=decode_case(case,'unused','unused')
        self.assertEqual((system,document),('custom system','custom document'))

    def test_incomplete_override_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_case(('n','old','new','BLUE','RED','question','system'),'', '')


if __name__=='__main__':unittest.main()
