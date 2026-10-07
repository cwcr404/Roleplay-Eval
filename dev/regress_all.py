# 全量离线回归聚合驱动(不烧 API;UTF-8 输出到 stdout 供脚本捕获)
import os, sys, json, tempfile, importlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['PYTHONIOENCODING'] = 'utf-8'

def main():
    os.environ['KB_DATA_ROOT'] = os.environ.get('KB_DATA_ROOT') or tempfile.mkdtemp(prefix='regress_')

    results = {}

    # --- session.acceptance_probe 五条 A-E ---
    from session.acceptance_probe import (
        _path_a_zhanghaogan, _path_b_fanggezi, _path_c_chongshen,
        _path_d_settle_duiyue, _path_e_discount80)
    for name, fn in [('A', _path_a_zhanghaogan), ('B', _path_b_fanggezi),
                     ('C', _path_c_chongshen), ('D', _path_d_settle_duiyue),
                     ('E', _path_e_discount80)]:
        _, r = fn()
        results['acceptance_' + name] = r

    results['session_selfcheck'] = _run_py('-m', 'session.selfcheck')
    results['kb_selfcheck'] = _run_py('kb/selfcheck.py')
    results['kb_memory_selfcheck'] = _run_py('kb/memory_selfcheck.py')
    results['routing_block2'] = _run_py('-m', 'session.routing_probe')
    results['marker_mid_observability'] = _run_py('-m', 'session.marker_mid_probe')
    results['distill_demo'] = _run_py('-m', 'session.distill_demo')
    results['distill_timetravel'] = _run_py('-m', 'session.distill_timetravel_check')
    results['decay_purefunc'] = _run_py('-m', 'session.decay_probe')
    results['emotion_channel'] = _run_py('session/emotion_selftest.py')
    results['roles_channel'] = _run_py('session/roles_selftest.py')
    results['world_model'] = _run_py('session/world_selftest.py')
    results['l3_boundary'] = _run_py('session/l3_selftest.py')

    ok = all(results.values())
    for k, v in results.items():
        print(('PASS  ' if v else 'FAIL  ') + k)
    print('\nALL_GREEN=', ok)
    return 0 if ok else 1


def _run_py(*args):
    import subprocess
    env = dict(os.environ)
    env['PYTHONPATH'] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, *args],
                       capture_output=True, text=True, encoding='utf-8', env=env)
    return p.returncode == 0


if __name__ == '__main__':
    raise SystemExit(main())
