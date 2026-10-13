import json
import os
from pathlib import Path
import sys
import unittest

resources = Path(os.environ['LOCALAPPDATA']) / 'Programs/personal-computer-use/resources'
sys.path.insert(0, str(resources))
import backend
assert Path(backend.__file__).parent == resources / 'backend'
tests = Path.home() / 'Downloads/pcu-unit-tests'
suite = unittest.defaultTestLoader.discover(str(tests), pattern='test_*.py')
result = unittest.TextTestRunner(verbosity=1).run(suite)
report = {'testsRun':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
          'skipped':len(result.skipped),'installedModules':True,'python':sys.version.split()[0]}
(Path.home() / 'Downloads/pcu-vm-evidence/python-tests.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report),flush=True)
sys.exit(not result.wasSuccessful())
