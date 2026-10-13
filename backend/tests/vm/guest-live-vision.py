import contextlib, io, json, os, runpy, sys, time
from pathlib import Path
install=Path(os.environ['LOCALAPPDATA'])/'Programs'/'personal-computer-use'
sys.path.insert(0,str(install/'resources'))
os.environ['PCU_CODEX_EXECUTABLE']=str(install/'resources'/'codex-runtime'/'codex.exe')
import backend
assert Path(backend.__file__).is_relative_to(install)
output=io.StringIO();started=time.monotonic()
report={'installedVersion':'1.1.3','model':'gpt-6-luna','installedProvider':True,'authentication':'temporary reuse of owner session in disposable VM'}
try:
    with contextlib.redirect_stdout(output):runpy.run_path(str(Path.home()/'Downloads'/'test_codex_live.py'),run_name='__main__')
    report['pass']=True
except Exception as e:
    report['pass']=False;report['error']=str(e)
report['seconds']=round(time.monotonic()-started,1);report['checks']=output.getvalue().splitlines()
(Path.home()/'Downloads'/'pcu-vm-evidence'/'live-vision.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report),flush=True)
assert report['pass']
